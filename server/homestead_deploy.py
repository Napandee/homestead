"""Deploy: a new app from the form, the App Store or a Compose file, and the
preview that reviews it first.

server.py used to answer all of this in place (#363). The routes are declared
here now; the helpers they still share with server.py's other code are bound
by name (bind), so a test that replaces one in server.py replaces it here too.
"""
import homestead_capacity_review as CAPACITY_REVIEW
import homestead_routes as ROUTER

# server.py's helpers these routes call, bound by name at start.
NEEDS = ("kget", "reviewed_deploy", "deploy_options", "compose_report", "compose_preview", "compose_apply",
         "fetch_appstore", "template_to_cfg", "catalog_source", "appstore_key", "appstore_summary",
         "rank_appstore", "search_appstore", "appstore_spotlight", "analyze_deploy_intent", "_dns_name",
         "rollout_review_context", "deploy_capacity_plan", "prepare_deploy_network", "apply_deploy_bindings",
         "apply_generated_secrets", "build_deployment", "build_sidecar_deployment", "redact_deployment_preview")
# And its values: where apps go by default, and the catalogue's own feed.
DEFAULT_NS = "lab"
CA_FEED = ""


def bind(helpers, *, default_ns, ca_feed):
    global DEFAULT_NS, CA_FEED
    missing = set(NEEDS) - set(helpers)
    if missing:
        raise TypeError("homestead_deploy needs " + ", ".join(sorted(missing)))
    globals().update({name: helpers[name] for name in NEEDS})
    DEFAULT_NS, CA_FEED = default_ns, ca_feed


def _arg(request, key, default=""):
    return (request.query.get(key) or [default])[0]


def _catalogue():
    """The App Store's apps, or the 502 that says its feed did not answer."""
    try:
        return fetch_appstore(), None
    except Exception as e:
        return None, ROUTER.Reply(502, {"error": f"app feed unavailable: {e}"})


def appstore(request):
    term = _arg(request, "q").lower().strip()
    cat = _arg(request, "cat").lower().strip()
    sort_mode = _arg(request, "sort", "home").lower().strip()
    if sort_mode not in {"home", "spotlight", "popular", "trending", "recent"}:
        sort_mode = "home"
    apps, refused = _catalogue()
    if refused:
        return refused
    source = {"url": catalog_source(), "default": catalog_source() == CA_FEED}
    if sort_mode == "home" and not term and not cat:
        # The catalogue's front page, as Community Applications lays it out.
        return {"sort": "home", "total": len(apps), "source": source, "sections": {
            mode: [appstore_summary(a) for a in rank_appstore(apps, mode)[:count]]
            for mode, count in (("spotlight", 4), ("recent", 8), ("trending", 8), ("popular", 8))}}
    if sort_mode == "home":
        sort_mode = "popular"
    if term:
        apps = search_appstore(apps, term)
    else:
        apps = rank_appstore(apps, sort_mode)
    if cat:
        apps = [a for a in apps if any(cat in value.lower() for value in a.get("categories", []))]
    spotlight = None if term else appstore_spotlight(apps)
    limit = 60 if term else 30
    return {"total": len(apps), "apps": [appstore_summary(a) for a in apps[:limit]],
            "sort": "search" if term else sort_mode, "source": source,
            "spotlight": appstore_summary(spotlight) if spotlight else None}


def appstore_app(request):
    key = _arg(request, "key")
    try:
        app = next((a for a in fetch_appstore() if appstore_key(a) == key), None)
    except Exception as e:
        return ROUTER.Reply(502, {"error": f"app feed unavailable: {e}"})
    if not app:
        return ROUTER.Reply(404, {"error": "that app is no longer in the catalogue"})
    return {k: v for k, v in app.items() if k != "config"}


def appstore_install(request):
    b = request.body
    cfg = template_to_cfg(b["app"])
    cfg.update(b.get("overrides") or {})
    # Older API clients must use the same review as the current UI.
    # The token is issued by /api/preview for the resolved template
    # plus overrides, not for the catalogue identifier alone.
    cfg.update(capacity_token=b.get("capacity_token"), confirm_capacity=b.get("confirm_capacity") is True)
    result = reviewed_deploy(cfg)
    reused = result.get("reused_volumes") or []
    if reused:
        result["detail"] = f"kept the existing {', '.join(reused)} - its data carries on"
    return result


def preview(request):
    """What Deploy would create, and its capacity review, without creating it."""
    b = analyze_deploy_intent(request.body)
    current = None
    context = None
    if b.get("target_mode") == "existing":
        ns = _dns_name(b.get("namespace") or DEFAULT_NS, "namespace")
        target = _dns_name(b.get("target_workload"), "existing workload")
        current = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{target}")
        context = rollout_review_context(current)
    capacity = deploy_capacity_plan(b, existing=current)
    capacity_token = CAPACITY_REVIEW.issue(b, context)
    b = prepare_deploy_network(b)
    b = apply_deploy_bindings(b)
    b = apply_generated_secrets(b)
    if b.get("target_mode") == "existing":
        target = _dns_name(b.get("target_workload"), "existing workload")
        dep, svc = build_sidecar_deployment(b, current)
        return {"deployment": redact_deployment_preview(dep, b), "service": svc,
                "capacity": capacity, "capacity_token": capacity_token,
                "app_profile": b.get("app_profile"),
                "impact": {"mode": "existing", "workload": target,
                           "containers": [c.get("name") for c in current.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])],
                           "message": "Saving updates the Deployment template and restarts every container in its pods."}}
    dep, svc = build_deployment(b)
    return {"deployment": redact_deployment_preview(dep, b), "service": svc,
            "capacity": capacity, "capacity_token": capacity_token,
            "app_profile": b.get("app_profile"),
            "impact": {"mode": "new", "workload": dep["metadata"]["name"],
                       "message": "Creates a new independently managed Deployment."}}


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/deploy/options"): ("viewer", lambda request: deploy_options(_arg(request, "ns", DEFAULT_NS))),
    ("GET", "/api/appstore"): ("viewer", appstore),
    ("GET", "/api/appstore/app"): ("viewer", appstore_app),
    ("POST", "/api/appstore/install"): ("operator", appstore_install),
    ("POST", "/api/preview"): ("operator", preview),
    ("POST", "/api/deploy"): ("operator", lambda request: reviewed_deploy(request.body)),
    ("POST", "/api/compose/parse"): ("operator", lambda request: compose_report(request.body)),
    ("POST", "/api/compose/preview"): ("operator", lambda request: compose_preview(request.body)),
    ("POST", "/api/compose/apply"): ("operator", lambda request: compose_apply(request.body)),
}
