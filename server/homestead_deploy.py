"""Deploy: a new app from the form, the App Store or a Compose file, and the
preview that reviews it first.

server.py used to answer all of this in place (#363). The routes are declared
here now; the helpers they still share with server.py's other code are bound
by name (bind), so a test that replaces one in server.py replaces it here too.
"""
import copy
import html
import json
import os
import re
import time
import urllib.request

import homestead_batch_capacity as BATCH_CAPACITY
import homestead_capacity_review as CAPACITY_REVIEW
import homestead_env_secrets as ENVSEC
import homestead_failover as FAILOVER
import homestead_hardware as HW
import homestead_host_access as HOSTACCESS
import homestead_lan as LAN
import homestead_lifecycle as LC
import homestead_memory as MEMORY
import homestead_names as NAMES
import homestead_networking as NETWORK
import homestead_operations as OPS
import homestead_place as PLACE
import homestead_platform as PLATFORM
import homestead_priority as PRIORITY
import homestead_privileges as PRIV
import homestead_rollout_capacity as ROLLOUT_CAPACITY
import homestead_routes as ROUTER
import homestead_updates as UPDATES
import homestead_uptime as UPTIME
import homestead_volowner as VOLOWNER

# server.py's helpers this module calls, bound by name at start.
NEEDS = ("kget", "ksend", "cached", "get_app_settings", "guard_managed_smb", "reviewed_deploy", "deploy_options",
         "compose_report", "compose_preview", "compose_apply", "_dns_name", "rollout_review_context",
         "deploy_capacity_plan", "prepare_deploy_network", "apply_deploy_bindings", "apply_generated_secrets",
         "redact_deployment_preview", "additional_container_configs", "deployment_volumes", "new_claims",
         "ensure_claim", "ensure_profile_compatible", "persist_icon_config", "prepare_lan", "record_lan")
# And its values: where apps go by default, and the release the App Store feed is asked for as.
DEFAULT_NS = "lab"
HOMESTEAD_VERSION = ""


def bind(helpers, *, default_ns, version):
    global DEFAULT_NS, HOMESTEAD_VERSION
    missing = set(NEEDS) - set(helpers)
    if missing:
        raise TypeError("homestead_deploy needs " + ", ".join(sorted(missing)))
    globals().update({name: helpers[name] for name in NEEDS})
    DEFAULT_NS, HOMESTEAD_VERSION = default_ns, version



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


# ---------------------------------------------------------------- app store
CA_FEED = os.environ.get(
    "COMMUNITY_CATALOG_URL",
    "https://raw.githubusercontent.com/Squidly271/AppFeed/master/applicationFeed.json",
)


def category_values(value):
    """Flatten inconsistent feed category shapes into stable display strings."""
    found = []

    def visit(item):
        if isinstance(item, (list, tuple)):
            for child in item:
                visit(child)
        elif isinstance(item, dict):
            preferred = [item.get(key) for key in ("name", "label", "category", "Category")]
            usable = [child for child in preferred if child not in (None, "")]
            for child in usable or item.values():
                visit(child)
        elif item is not None:
            for part in re.split(r"[\s,|]+", str(item).strip()):
                if part and part not in found:
                    found.append(part)

    visit(value)
    return found


# Markup in catalogue text: HTML tags, and forum codes such as [b], [/span] and
# [span style='color: red'] - a known tag name, so "[1]" or "[x86]" survive.
CATALOG_MARKUP = re.compile(
    r"<[^>]*>|\[/?(?:b|i|u|s|br|p|hr|img|url|span|color|size|font|center|left|right|quote|code|list|li|\*|h[1-6])"
    r"(?:[= ][^\]]*)?\]", re.I)


def catalog_text(value, limit=None):
    """Turn catalogue HTML fragments and entities into compact readable text."""
    text = str(value or "")
    # Some catalogue fields contain encoded markup (and occasionally encode it
    # twice), so decode before removing tags.
    for _ in range(2):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    text = re.sub(r"(?i)<\s*br\s*/?\s*>|\[\s*br\s*/?\s*\]", "\n", text)
    text = re.sub(r"(?i)</\s*(?:p|div|li|tr|h[1-6])\s*>", "\n", text)
    # HTML, and the forum's [b]/[span style=...] codes that templates use too.
    text = re.sub(CATALOG_MARKUP, "", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\s*\n+\s*", " · ", text)
    text = re.sub(r"(?:\s*·\s*)+", " · ", text).strip(" ·")
    return text[:limit] if limit is not None else text


def catalog_paragraphs(value, limit=8000):
    """Catalogue text with its paragraphs kept, for reading in full.

    Overviews mix HTML, entities and the forum's [b]/[br] codes; each becomes
    plain text with line breaks where the author put them."""
    text = str(value or "")
    for _ in range(2):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    text = re.sub(r"(?i)\[\s*br\s*/?\s*\]|<\s*br\s*/?\s*>", "\n", text)
    text = re.sub(r"(?i)</\s*(?:p|div|li|tr|h[1-6])\s*>", "\n", text)
    text = re.sub(CATALOG_MARKUP, "", text)
    text = html.unescape(text).replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit]


def catalog_reason(value):
    """A spotlight's reason, which the feed gives as a dict - sometimes as its text."""
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            import ast
            value = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            pass
    if isinstance(value, dict):
        value = value.get("en_US") or next(iter(value.values()), "")
    return catalog_text(value)


def catalog_links(value):
    if isinstance(value, str):
        value = [value]
    return [str(x) for x in (value or []) if isinstance(x, str) and x.startswith(("http://", "https://"))][:6]


def appstore_key(app):
    return f"{app.get('name')}|{app.get('repo')}"


# What a list of apps needs; the full record comes one app at a time.
APPSTORE_HEAVY = ("overview", "config", "screenshots", "readme", "comment", "requires")


def appstore_summary(app):
    return {k: v for k, v in app.items() if k not in APPSTORE_HEAVY}


def search_appstore(apps, term):
    """Return catalogue matches with name relevance ahead of description hits."""
    needle = str(term or "").strip().lower()
    if not needle:
        return list(apps)

    def relevance(app):
        name = str(app.get("name") or "").lower()
        description = str(app.get("desc") or "").lower()
        if name == needle:
            return 0
        if name.startswith(needle):
            return 1
        if re.search(rf"(?:^|[^a-z0-9]){re.escape(needle)}", name):
            return 2
        if needle in name:
            return 3
        if needle in description:
            return 4
        return None

    ranked = []
    for position, app in enumerate(apps):
        score = relevance(app)
        if score is not None:
            ranked.append((score, position, app))
    return [app for _, _, app in sorted(ranked, key=lambda row: (row[0], row[1]))]


def appstore_number(value):
    """Coerce optional feed statistics without letting malformed rows break browsing."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def unique_appstore_apps(apps):
    """Hide duplicate templates that point at the same named container image."""
    seen, out = set(), []
    for app in apps:
        key = (str(app.get("name") or "").strip().lower(),
               str(app.get("repo") or "").strip().lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(app)
    return out


def rank_appstore(apps, mode="popular"):
    """Rank cached catalogue rows using only statistics supplied by the feed."""
    rows = unique_appstore_apps(apps)
    if mode == "spotlight":
        return sorted((app for app in rows if app.get("spotlight")),
                      key=lambda app: -app["spotlight"]["date"])
    if mode == "recent":
        # Templates added in one feed update share a timestamp; the feed's own
        # order among them is the one Community Applications shows.
        key = lambda app: -appstore_number(app.get("first_seen"))
    elif mode == "trending":
        key = lambda app: (-appstore_number(app.get("top_trending")),
                           -appstore_number(app.get("trending")),
                           -appstore_number(app.get("top_performing")),
                           -appstore_number(app.get("downloads")),
                           str(app.get("name") or "").lower())
    else:
        key = lambda app: (-appstore_number(app.get("top_performing")),
                           -appstore_number(app.get("trending")),
                           -appstore_number(app.get("top_trending")),
                           -appstore_number(app.get("downloads")),
                           str(app.get("name") or "").lower())
    return sorted(rows, key=key)


def appstore_spotlight(apps):
    """Choose the strongest feed-ranked app for the catalogue spotlight."""
    ranked = rank_appstore(apps, "popular")
    return next((app for app in ranked if appstore_number(app.get("top_performing")) > 0),
                next((app for app in ranked if appstore_number(app.get("trending")) > 0),
                     ranked[0] if ranked else None))


def catalog_source():
    """The catalogue feed in use: the one set in Settings, else the default."""
    try:
        custom = cached("settings", 15, get_app_settings).get("catalog_url") or ""
    except Exception:
        custom = ""
    return custom or CA_FEED


def fetch_appstore():
    source = catalog_source()

    def go():
        req = urllib.request.Request(source, headers={
            "User-Agent": f"Homestead/{HOMESTEAD_VERSION} (+https://github.com/homestead-lab/homestead)",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        apps = data.get("applist", data if isinstance(data, list) else [])
        out = []
        for a in apps:
            repo = a.get("Repository") or ""
            if not repo or not a.get("Name"):
                continue
            # Containers only, as Community Applications shows them: a plugin
            # or language pack is Unraid's own software, and blacklisted,
            # deprecated or hidden templates are not offered there either.
            if (a.get("Plugin") or a.get("PluginURL") or a.get("Language") or a.get("LanguagePack")
                    or a.get("Blacklist") or a.get("Deprecated") or a.get("hideFromCA")
                    or str(repo).lower().endswith(".plg")):
                continue
            spotlight = None
            if appstore_number(a.get("RecommendedDate")):
                stamp = int(appstore_number(a.get("RecommendedDate")))
                spotlight = {"date": stamp, "month": time.strftime("%b %Y", time.gmtime(stamp)),
                             "reason": catalog_reason(a.get("RecommendedReason")),
                             "who": catalog_text(a.get("RecommendedWho") or "")}
            maintainer = a.get("Maintainer") or a.get("Author") or ""
            if isinstance(maintainer, dict):
                maintainer = maintainer.get("Name") or next((v for v in maintainer.values() if isinstance(v, str)), "")
            maintainer = str(maintainer or re.sub(r"'s Repository$", "", str(a.get("Repo") or ""))).strip()
            categories = category_values(a.get("CategoryList") or a.get("Category") or "")
            item = {
                "name": a.get("Name"),
                "repo": repo,
                "icon": a.get("Icon") or "",
                "desc": catalog_text(a.get("Overview") or a.get("Description") or "", 300),
                "cat": categories[0] if categories else "",
                "categories": categories,
                "web": a.get("Project") or a.get("Support") or "",
                "network": a.get("Network") or "bridge",
                "webui": a.get("WebUI") or "",
                "config": a.get("Config") or [],
                "downloads": int(appstore_number(a.get("downloads"))),
                "stars": int(appstore_number(a.get("stars"))),
                "trending": appstore_number(a.get("trending")),
                "top_trending": appstore_number(a.get("topTrending")),
                "top_performing": appstore_number(a.get("topPerforming")),
                "first_seen": int(appstore_number(a.get("FirstSeen"))),
                "last_update": int(appstore_number(a.get("LastUpdate"))),
                "spotlight": spotlight,
                "maintainer": catalog_text(maintainer, 80),
                "official": bool(a.get("Official") or a.get("LTOfficial")),
                "beta": str(a.get("Beta") or "").lower() in ("true", "1", "yes"),
                "privileged": str(a.get("Privileged") or "").lower() == "true",
                "extra_params": catalog_text(a.get("ExtraParams") or "", 600),
                "links": {k: v for k, v in {
                    "project": a.get("Project"), "support": a.get("Support"), "registry": a.get("Registry"),
                    "readme": a.get("ReadMe") or a.get("Readme"), "video": a.get("Video"),
                    "discord": a.get("Discord"), "github": a.get("GitHub"), "web": a.get("WebPage"),
                }.items() if isinstance(v, str) and v.startswith(("http://", "https://"))},
                "overview": catalog_paragraphs(a.get("Overview") or a.get("Description") or ""),
                "screenshots": catalog_links(a.get("Screenshot")),
                "requires": catalog_text(a.get("Requires") or "", 600),
                "comment": catalog_text(a.get("CAComment") or a.get("ModeratorComment") or "", 600),
                "license": catalog_text(a.get("License") or a.get("Licence") or "", 80),
            }
            item["deploy"] = template_to_cfg(item)
            out.append(item)
        return out
    return cached(f"appstore:{source}", 21600, go)


def appdata_folders(vols):
    """Gives each path an app keeps in its appdata volume a folder of its own.

    An Unraid template maps each path to its own host folder; here they share
    one volume, as an import lays them out, each mounted from its own folder
    with subPath. A single path takes the whole volume. The volume is sized
    for everything in it."""
    shared = [v for v in vols if v.get("type") == "pvc" and v.get("create")]
    if len(shared) < 2:
        return vols
    taken, total = set(), 0
    access = "ReadWriteMany" if any(v.get("access_mode") == "ReadWriteMany" for v in shared) else "ReadWriteOnce"
    for v in shared:
        base = re.sub(r"[^a-z0-9._-]+", "-", str(v["path"]).rstrip("/").rsplit("/", 1)[-1].lower()).strip("-.") or "data"
        folder, n = base, 2
        while folder in taken:
            folder, n = f"{base}-{n}", n + 1
        taken.add(folder)
        v["sub_path"] = folder
        total += int(v.get("size_gb") or 5)
        v["access_mode"] = access
    for v in shared:
        v["size_gb"] = total
    return vols


def template_to_cfg(app):
    """Turn an Unraid CA template entry into our deploy config."""
    ports, envs, env_meta, vols, devices = [], {}, [], [], []
    cfgs = app.get("config") or []
    if isinstance(cfgs, dict):
        cfgs = [cfgs]
    if not isinstance(cfgs, list):
        cfgs = []
    name = re.sub(r"[^a-z0-9-]", "-", app["name"].lower()).strip("-")[:40]
    for c in cfgs:
        if not isinstance(c, dict):
            continue
        attrs = c.get("@attributes", {}) or {}
        typ = str(attrs.get("Type") or c.get("Type") or "").strip().lower()
        tgt = str(attrs.get("Target") or c.get("Target") or "").strip()
        val = c.get("value")
        if val is None or val == "":
            val = attrs.get("Default") or c.get("Default") or ""
        val = str(val)
        label = catalog_text(attrs.get("Name") or c.get("Name") or tgt)
        description = catalog_text(attrs.get("Description") or c.get("Description") or "")
        required = str(attrs.get("Required") or c.get("Required") or "false").lower() == "true"
        mode = str(attrs.get("Mode") or c.get("Mode") or "")
        read_only = mode.lower() == "ro"
        if typ == "port" and tgt:
            try:
                protocol = mode.upper() if mode.lower() in ("tcp", "udp") else "TCP"
                ports.append({"container": int(tgt), "host": int(val or tgt), "expose": True,
                              "name": f"p{tgt}-{protocol.lower()}", "protocol": protocol,
                              "label": label, "description": description, "required": required})
            except (TypeError, ValueError):
                pass
        elif typ == "variable" and tgt:
            options = [option.strip() for option in val.split("|") if option.strip()] if "|" in val else []
            if options:
                val = options[0]
            masked = str(attrs.get("Mask", "false")).lower() == "true"
            secret_value = masked or bool(re.search(r"(?:PASSWORD|PASS|TOKEN|SECRET|API_KEY|APIKEY)$", tgt, re.I))
            generate = secret_value and (bool(val) or required or (masked and "password" in tgt.lower()))
            if generate:
                # Public catalogue defaults must never become deployed credentials.
                val = ""
            envs[tgt] = val
            env_meta.append({"key": tgt, "label": label, "description": description,
                             "required": required, "masked": secret_value,
                             "generate": generate, "options": options})
        elif typ == "path" and tgt:
            system_bind = tgt in ("/etc/localtime", "/var/run/docker.sock") and val == tgt
            clean_path = tgt.rstrip("/").lower() or "/"
            context = " ".join((clean_path, label, description, val)).lower()
            tokens = set(re.split(r"[^a-z0-9]+", context))
            cache_tokens = {"cache", "caches", "transcode", "transcoding", "temp", "temporary", "tmp"}
            media_tokens = {"media", "movie", "movies", "tv", "music", "photo", "photos",
                            "video", "videos", "recording", "recordings", "download", "downloads"}
            config_path = (clean_path == "/config" or clean_path.endswith("/config") or
                           "appdata" in str(val).lower())
            role = ("system" if system_bind else "cache" if tokens & cache_tokens else
                    "config" if config_path else "media" if tokens & media_tokens else
                    "data" if clean_path == "/data" or clean_path.endswith("/data") else "config")
            volume_type = "host" if system_bind else "emptyDir" if role == "cache" else "pvc"
            create = not system_bind and role not in ("cache", "media")
            size = 50 if role == "data" else 5
            access_mode = ("ReadWriteMany" if {"rwx", "shared", "multinode", "multi-node"} & tokens
                           else "ReadWriteOnce")
            source = (val if system_bind else "" if role in ("cache", "media") else f"{name}-appdata")
            vols.append({"path": tgt, "source": source,
                         "type": volume_type, "create": create, "role": role,
                         "access_mode": access_mode, "size_gb": size,
                         "read_only": read_only, "label": label, "description": description,
                         "required": required, "template_source": val})
        elif typ == "device":
            devices.append({"host_path": val or tgt, "container_path": tgt or val,
                            "label": label, "description": description, "required": required})
    appdata_folders(vols)
    network = str(app.get("network") or "bridge").strip().lower()
    network_mode = "host" if network == "host" else "loadbalancer"
    vip_mode = "auto" if network not in ("bridge", "host", "default", "") else "shared"

    # Host-mode templates frequently omit port rows. Preserve the useful WebUI
    # listener so users can switch to a Service without re-reading the template.
    if not ports:
        match = re.search(r"\[PORT:(\d+)\]", str(app.get("webui") or ""), re.I)
        if match:
            port = int(match.group(1))
            ports.append({"container": port, "host": port, "expose": network != "host",
                          "name": f"p{port}-tcp", "protocol": "TCP",
                          "label": "Web interface", "description": "Inferred from the template WebUI URL",
                          "required": True})
    # What Unraid lets it do to its host: Privileged, and the capabilities and
    # tunnel device its extra parameters ask for (VPN containers need them).
    privileges = PRIV.from_docker(app.get("extra_params", ""), app.get("privileged"),
                                  devices=[d["host_path"] for d in devices])
    devices = [d for d in devices if PRIV.TUN not in (d["host_path"] + d["container_path"])]
    cfg = {"name": name,
            "image": app["repo"], "icon": app.get("icon") or "",
            "ports": ports, "env": envs, "env_meta": env_meta, "volumes": vols,
            **privileges,
            "template_devices": devices, "template_network": network,
            "network_mode": network_mode, "vip_mode": vip_mode,
            "env_bindings": {}}
    return analyze_deploy_intent(cfg)


def analyze_deploy_intent(cfg):
    """Derive portable Kubernetes guidance from template semantics, never app names."""
    updated = dict(cfg)
    updated["ports"] = [dict(item) for item in (cfg.get("ports") or [])]
    updated["env_bindings"] = dict(cfg.get("env_bindings") or {})
    notes, dependencies, intents = [], [], []
    exposed = [item for item in updated["ports"] if item.get("expose", True)]
    dns = any(int(item.get("container") or 0) == 53 for item in exposed)
    if dns and updated.get("network_mode") != "host":
        updated["network_mode"], updated["vip_mode"] = "loadbalancer", "auto"
        intents.append("network")
        notes.append("A dedicated automatic VIP is selected because this template exposes DNS port 53.")
        bind_names = {"SERVERIP", "SERVER_IP", "LOCAL_IPV4", "FTLCONF_LOCAL_IPV4"}
        for key in (updated.get("env") or {}):
            if re.sub(r"[^A-Z0-9_]", "", key.upper()) in bind_names:
                updated["env_bindings"][key] = "vip"
        if updated["env_bindings"]:
            notes.append("Address variables are filled from the allocated VIP at deploy time.")
    for port in updated["ports"]:
        if int(port.get("container") or 0) == 67 and str(port.get("protocol") or "TCP").upper() == "UDP":
            port["expose"], port["required"] = False, False
            port["description"] = "Optional: expose only when this workload provides DHCP"
            if "network" not in intents:
                intents.append("network")
            notes.append("DHCP port 67 stays disabled unless you explicitly expose it.")

    volumes = updated.get("volumes") or []

    def inferred_role(item):
        if item.get("role"):
            return item["role"]
        path = str(item.get("path") or "").rstrip("/").lower() or "/"
        context = " ".join(str(item.get(key) or "") for key in
                           ("path", "source", "template_source", "label", "description")).lower()
        tokens = set(re.split(r"[^a-z0-9]+", context))
        if path in ("/etc/localtime", "/var/run/docker.sock", "/run/containerd/containerd.sock"):
            return "system"
        if tokens & {"cache", "caches", "transcode", "transcoding", "temp", "temporary", "tmp"}:
            return "cache"
        if path == "/config" or path.endswith("/config") or "appdata" in context:
            return "config"
        if tokens & {"media", "movie", "movies", "tv", "music", "photo", "photos", "video",
                     "videos", "recording", "recordings", "download", "downloads"}:
            return "media"
        if path == "/data" or path.endswith("/data"):
            return "data"
        return "config"

    media = [item.get("path") for item in volumes if inferred_role(item) == "media"]
    cache = [item.get("path") for item in volumes if inferred_role(item) == "cache"]
    data_paths = [item.get("path") for item in volumes if inferred_role(item) == "data"]
    if media:
        intents.append("storage")
        notes.append("Choose existing/shared media storage for: " + ", ".join(filter(None, media)) + ".")
    if cache:
        if "storage" not in intents:
            intents.append("storage")
        notes.append("Temporary pod storage is selected for cache/transcode paths: " + ", ".join(filter(None, cache)) + ".")
    if data_paths:
        if "storage" not in intents:
            intents.append("storage")
        notes.append("Persistent data paths start as editable Longhorn claims; choose RWX when multiple replicas or workloads must attach: " + ", ".join(filter(None, data_paths)) + ".")
    if any(item.get("access_mode") == "ReadWriteMany" for item in volumes):
        if "storage" not in intents:
            intents.append("storage")
        notes.append("Template wording indicates shared storage; review the proposed RWX claim and size.")

    generated = [item.get("key") for item in (updated.get("env_meta") or []) if item.get("generate")]
    if generated:
        intents.append("security")
        notes.append("Public defaults for secret fields are discarded and generated locally.")
    option_fields = [item.get("key") for item in (updated.get("env_meta") or []) if item.get("options")]
    if option_fields:
        notes.append("Enumerated template values are presented as selectors instead of literal option strings.")

    env_keys = {re.sub(r"[^A-Z0-9_]", "", key.upper()) for key in (updated.get("env") or {})}
    if any(key.endswith(("DB_HOST", "DATABASE_HOST", "MYSQL_HOST", "POSTGRES_HOST")) for key in env_keys):
        dependencies.append({"kind": "database", "name": "External database endpoint",
                             "required": True, "managed": False})
    if any(key.endswith(("REDIS_HOST", "CACHE_HOST")) for key in env_keys):
        dependencies.append({"kind": "cache", "name": "External cache endpoint",
                             "required": True, "managed": False})

    runtime_socket = next((item for item in volumes
                           if item.get("path") in ("/var/run/docker.sock", "/run/containerd/containerd.sock")), None)
    blocked = bool(runtime_socket)
    if blocked:
        intents.append("safety")
        dependencies.append({"kind": "runtime", "name": "Host container-runtime control",
                             "required": True, "managed": False})
        notes.append("This template requests a host container-runtime socket and can create or control other containers; it needs a Kubernetes-specific deployment design.")
    if dependencies:
        intents.append("dependency")
        notes.append("Review the external services listed below before deployment.")
    if updated.get("template_devices"):
        intents.append("hardware")
        notes.append("Imported device paths are matched to reusable hardware features; verify eligible hosts before deploying.")
    network = str(updated.get("template_network") or "bridge").lower()
    if network == "host":
        intents.append("network")
        notes.append("The source requests host networking; review node port collisions and failover, or switch to a Service VIP.")
    elif network not in ("bridge", "default", "") and not dns:
        intents.append("network")
        notes.append("The source custom network is represented by a dedicated Kubernetes VIP.")
    if not notes:
        notes.append("Review the imported ports, variables, and storage choices before deploying.")

    intents = list(dict.fromkeys(intents)) or ["template"]
    level = "dependency" if blocked or dependencies else "guided" if dns else "review"
    label = ("Needs Kubernetes design" if blocked else "Dependency review" if dependencies else
             "Dedicated VIP" if dns else "Storage review" if "storage" in intents else "Template review")
    updated["app_profile"] = {"intent": intents[0], "intents": intents, "level": level,
                              "label": label, "notes": notes, "dependencies": dependencies,
                              "blocked": blocked}
    return updated


def _build_single_deployment(cfg):
    name = _dns_name(cfg.get("workload_name") or cfg.get("name"), "workload name")
    container_name = _dns_name(cfg.get("container_name") or cfg.get("name"), "container name")
    ns = cfg.get("namespace", DEFAULT_NS)
    env = [{"name": k, "value": str(v)} for k, v in (cfg.get("env") or {}).items()]
    mounts, volumes, named, owned = [], [], {}, []
    for i, v in enumerate(cfg.get("volumes") or []):
        if v.get("type") == "pod":
            raise ValueError("existing pod volumes can only be used when joining an existing workload")
        # Several folders of one claim share a single volume entry and differ
        # only by subPath, which is how an import lands one volume per app.
        key = (v.get("type") or "pvc", v.get("source") or "")
        reusable = key[0] in ("pvc", "host") and key[1]
        vn = named.get(key) if reusable else None
        if not vn:
            vn = f"vol{len(volumes)}"
            if reusable:
                named[key] = vn
            if v.get("type") == "host":
                volumes.append({"name": vn, "hostPath": {"path": v["source"]}})
            elif v.get("type") == "emptyDir":
                # A RAM-backed scratch volume is what a tmpfs mount becomes:
                # same speed, same volatility, and bounded so it cannot eat the
                # node's memory.
                empty = {}
                if str(v.get("medium", "")).lower() == "memory":
                    empty["medium"] = "Memory"
                if v.get("size_limit"):
                    empty["sizeLimit"] = str(v["size_limit"])
                volumes.append({"name": vn, "emptyDir": empty})
            else:
                volumes.append({"name": vn, "persistentVolumeClaim": {"claimName": v["source"]}})
        mount = {"name": vn, "mountPath": v["path"]}
        owner = (cfg.get("volume_owners") or {}).get(v.get("path"))
        if owner and v.get("type") == "pvc" and v.get("create"):
            owned.append((vn, str(v.get("sub_path") or ""), *owner))
        if v.get("sub_path"):
            mount["subPath"] = str(v["sub_path"]).strip("/")
        if v.get("read_only"):
            mount["readOnly"] = True
        mounts.append(mount)
    ports = [{"containerPort": int(p["container"]),
              "name": (p.get("name") or f"p{p['container']}-{str(p.get('protocol', 'TCP')).lower()}")[:15],
              "protocol": str(p.get("protocol", "TCP")).upper()}
             for p in cfg.get("ports") or []]
    c = {"name": container_name, "image": UPDATES.with_tag(cfg["image"]), "imagePullPolicy": "IfNotPresent"}
    # Arguments to the image's own entrypoint - cloudflared's "tunnel run",
    # say. A list of plain strings, passed as they are; never a shell.
    args = cfg.get("args") or []
    if not isinstance(args, list) or len(args) > 32 or any(not isinstance(a, str) or len(a) > 512 or "\x00" in a for a in args):
        raise ValueError("arguments are a list of up to 32 short strings")
    if args: c["args"] = list(args)
    if env: c["env"] = env
    if ports: c["ports"] = ports
    if mounts: c["volumeMounts"] = mounts
    res = {}
    if cfg.get("cpu"): res.setdefault("requests", {})["cpu"] = cfg["cpu"]
    memory_request = str(cfg.get("memory") or "").strip()
    memory_limit = str(cfg.get("memory_limit") or "").strip()
    MEMORY.validate(memory_request, memory_limit, container_name)
    if memory_request: res.setdefault("requests", {})["memory"] = memory_request
    if memory_limit: res.setdefault("limits", {})["memory"] = memory_limit
    if res: c["resources"] = res
    if cfg.get("privileged"): c["securityContext"] = {"privileged": True}
    apply_container_settings(c, cfg)

    podspec = {"containers": [c], "automountServiceAccountToken": False}
    if volumes: podspec["volumes"] = volumes
    if owned:
        # A new volume is root's; the image may run as a user of its own.
        podspec["initContainers"] = [VOLOWNER.init_container(owned)]
    hardware = set(cfg.get("hardware") or [])
    if cfg.get("gpu"):
        hardware.add("igpu")
    devices = {f["id"]: HW.mount_spec(f) for f in HW.features()}
    unknown = hardware - set(devices)
    if unknown:
        raise ValueError("unknown hardware feature(s): " + ", ".join(sorted(unknown)))
    for hw in hardware:
        if hw not in devices:
            continue
        dev = devices[hw]
        podspec.setdefault("nodeSelector", {})[dev["label"]] = "true"
        podspec["containers"][0].setdefault("securityContext", {})["privileged"] = True
        podspec["containers"][0].setdefault("volumeMounts", []).append(
            {"name": dev["name"], "mountPath": dev["container_path"]})
        podspec.setdefault("volumes", []).append(
            {"name": dev["name"], "hostPath": {"path": dev["host_path"], "type": dev["path_type"]}})
    if any(key in cfg for key in ("privileged", "cap_add", "tun")):
        PRIV.apply(podspec["containers"][0], podspec, cfg, hardware=bool(hardware))
    if cfg.get("fs_group") is not None:
        # The kubelet gives the volume to this group and makes it group
        # writable, which is what lets a container that is not root write to
        # appdata it did not create.
        podspec.setdefault("securityContext", {})["fsGroup"] = int(cfg["fs_group"])
    if cfg.get("network_mode") == "host":
        podspec["hostNetwork"] = True
        podspec["dnsPolicy"] = "ClusterFirstWithHostNet"
    if cfg.get("node"):
        podspec.setdefault("nodeSelector", {})["kubernetes.io/hostname"] = cfg["node"]
    FAILOVER.apply(podspec, cfg.get("failover") or "move")
    lan_address = cfg.get("lan") if cfg.get("network_mode") == "lan" else None
    # How it is monitored, if not automatically: an annotation, read by homestead_uptime.
    monitoring = UPTIME.check_setting((cfg.get("monitoring") or {}).get("mode") or "auto",
                                      (cfg.get("monitoring") or {}).get("path"))
    dep = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": name, "namespace": ns, "labels": {"app": name, NAMES.key("managed"): "true"},
                     "annotations": ({**({NAMES.key("icon"): cfg.get("icon", "")} if cfg.get("icon") else {}),
                                      **({NAMES.key("icon-source"): cfg.get("icon_source", cfg.get("icon", ""))} if cfg.get("icon") else {}),
                                      **({NAMES.key("hardware"): ",".join(sorted(hardware))} if hardware else {}),
                                      **({NAMES.key("uptime"): monitoring} if monitoring else {})})},
        "spec": {"replicas": int(cfg.get("replicas", 1)), "strategy": {"type": "Recreate"},
                 "selector": {"matchLabels": {"app": name}},
                 "template": {"metadata": {"labels": {"app": name, "lab-workload": "true"}}, "spec": podspec}},
    }
    svc = None
    exposed = [p for p in cfg.get("ports") or [] if p.get("expose")]
    if exposed and cfg.get("network_mode") not in ("host", "lan"):
        mode = cfg.get("vip_mode", "shared")
        vip = cfg.get("lb_ip") if mode in ("manual", "automatic") else ((cfg.get("lb_ip") or NETWORK.shared_vip()) if mode == "shared" else "")
        svc_type = "ClusterIP" if cfg.get("network_mode") == "internal" else "LoadBalancer"
        svc = {
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": name, "namespace": ns, "labels": {"app": name, NAMES.key("managed"): "true"},
                         "annotations": PLATFORM.vip_annotations(vip) if svc_type == "LoadBalancer" else {}},
            "spec": {"type": svc_type, "selector": {"app": name},
                     **(PLATFORM.vip_spec(vip) if svc_type == "LoadBalancer" else {}),
                      "ports": [{"name": (p.get("name") or f"p{p['container']}-{str(p.get('protocol', 'TCP')).lower()}")[:15],
                                 "port": int(p.get("host") or p["container"]),
                                 "targetPort": int(p["container"]),
                                 "protocol": str(p.get("protocol", "TCP")).upper()} for p in exposed]},
        }
    if lan_address:
        LAN.apply_to_template(dep, ns, name, lan_address)
    return dep, svc


def deploy_tier(cfg):
    """The tier a new app is deployed at (#431): "critical" or "low", or "" for
    normal, which writes no class at all."""
    tier = cfg.get("priority")
    if tier in (None, "", "normal"):
        return ""
    if tier not in ("critical", "low"):
        raise ValueError("the tier must be critical, normal or low")
    return tier


def build_deployment(cfg):
    tier = deploy_tier(cfg)
    dep, service = _build_deployment_with_extras(cfg)
    if tier:
        LC._apply_priority(dep["spec"]["template"]["spec"], tier)
    return dep, service


def _build_deployment_with_extras(cfg):
    extras = additional_container_configs(cfg)
    if not extras:
        return _build_single_deployment(cfg)
    HOSTACCESS.require_cfg([cfg, *extras])
    dep, _ = _build_single_deployment(cfg)
    changes = [dict(item, new=True, name=item["container_name"], privileges={
        key: item[key] for key in ("privileged", "cap_add", "tun") if key in item}) for item in extras]
    prepared = LC.prepare_edit({"ns": dep["metadata"]["namespace"], "name": dep["metadata"]["name"],
                                "containers": changes}, current=dep)
    dep = prepared["deployment"]
    pspec = dep["spec"]["template"]["spec"]
    used_names = {container["name"] for group in ("containers", "initContainers") for container in pspec.get(group) or []}
    for item, container in zip(extras, pspec["containers"][1:]):
        mounts = {mount["mountPath"]: mount for mount in container.get("volumeMounts") or []}
        owners = [(mounts[path]["name"], mounts[path].get("subPath", ""), *owner)
                  for path, owner in (item.get("volume_owners") or {}).items() if path in mounts]
        if owners:
            helper = VOLOWNER.init_container(owners)
            helper["name"] = LC._unique_volume_name("hs-owner-" + container["name"], used_names)
            pspec.setdefault("initContainers", []).append(helper)
    all_ports = [port for item in [cfg, *extras] for port in item.get("ports") or []]
    service_ports = [dict(port, name=port.get("name") or f"p{port.get('host') or port['container']}-{str(port.get('protocol') or 'TCP').lower()}") for port in all_ports]
    _, service = _build_single_deployment(dict(cfg, ports=service_ports))
    if service:
        names = [port["name"] for port in service["spec"]["ports"]]
        if len(names) != len(set(names)):
            raise ValueError("exposed port names must be unique across all containers")
    return dep, service


def apply_container_settings(container, cfg):
    """Command, working directory, user and capabilities, when a config has them.

    Compose's entrypoint and command are Kubernetes' command and args: one
    replaces the image's ENTRYPOINT, the other its CMD.
    """
    for key, field in (("command", "command"), ("args", "args")):
        value = cfg.get(key)
        if value:
            if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                raise ValueError(f"{key} must be a list of words")
            container[field] = list(value)
    if cfg.get("working_dir"):
        container["workingDir"] = str(cfg["working_dir"])
    security = container.setdefault("securityContext", {})
    for key, field in (("run_as_user", "runAsUser"), ("run_as_group", "runAsGroup")):
        if cfg.get(key) is not None and cfg.get(key) != "":
            security[field] = int(cfg[key])
    if cfg.get("cap_add"):
        caps = [str(x).upper() for x in cfg["cap_add"]]
        if not all(re.fullmatch(r"[A-Z_]{2,40}", cap) for cap in caps):
            raise ValueError("capabilities are names like NET_ADMIN")
        security.setdefault("capabilities", {})["add"] = caps
    if not security:
        container.pop("securityContext", None)
    return container


def run_deploy(b, *, reviewed_current=None):
    """Create (or join) a workload. HTTP Deploy uses reviewed_deploy first."""
    b = analyze_deploy_intent(b)
    ns = b.get("namespace") or DEFAULT_NS
    target = b.get("target_workload") if b.get("target_mode") == "existing" else b.get("workload_name") or b.get("name")
    guard_managed_smb(ns, target)
    b = ensure_profile_compatible(b)
    persist_icon_config(b)
    b = prepare_deploy_network(b)
    b = apply_deploy_bindings(b)
    b = apply_generated_secrets(b)
    b = prepare_lan(b)
    ns = b.get("namespace") or DEFAULT_NS
    target_mode = b.get("target_mode", "new")
    if target_mode == "new":
        b = VOLOWNER.prepare(b)
        b["additional_containers"] = [VOLOWNER.prepare(item) for item in additional_container_configs(b)]
    if target_mode == "existing":
        target = _dns_name(b.get("target_workload"), "existing workload")
        current = copy.deepcopy(reviewed_current) if reviewed_current is not None else kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{target}")
        dep, svc = build_sidecar_deployment(b, current)
    elif target_mode == "new":
        dep, svc = build_deployment(b)
        target = dep["metadata"]["name"]
        # The class exists before the Deployment names it, as Edit does it.
        if deploy_tier(b):
            PRIORITY.ensure([deploy_tier(b)])
    else:
        raise ValueError("deployment target must be new or existing")
    reused = []
    for claim in new_claims(deployment_volumes(b)):
        if ensure_claim(ns, claim["name"], claim["size_gb"], claim["storage_class"], claim["access_mode"]):
            reused.append(claim["name"])
    b["_reused_claims"] = reused
    if b.get("network_mode") == "lan" and target_mode == "new":
        LAN.ensure_nad(ns, target, b["lan"])
    masked = {item.get("key") for item in b.get("env_meta") or [] if item.get("masked")}
    ENVSEC.externalize(ns, dep, kget, ksend, masked)
    if target_mode == "existing":
        ksend("PUT", f"/apis/apps/v1/namespaces/{ns}/deployments/{target}", dep)
    else:
        ksend("POST", f"/apis/apps/v1/namespaces/{ns}/deployments", dep)
    if b.get("network_mode") == "lan" and target_mode == "new":
        record_lan(ns, target, b["lan"])
    if svc:
        ksend("POST", f"/api/v1/namespaces/{ns}/services", svc)
    ROUTER.forget("wl", "ov", "network")
    container_name = _dns_name(b.get("container_name") or b.get("name"), "container name")
    action = f"Add {container_name} to {target}" if target_mode == "existing" else f"Deploy {target}"
    op = OPS.start("deployment", action,
                   {"kind": "Deployment", "name": target, "namespace": ns},
                   "/containers", {"namespace": ns, "name": target,
                                   # What cancelling it undoes: a new one is removed,
                                   # an existing one goes back to its last version.
                                   "undo": "rollout" if target_mode == "existing" else "delete"})
    return {"ok": True, "name": target, "container": container_name, "operation": op,
            "reused_volumes": b.get("_reused_claims") or []}


def compose_capacity(entries, claims, created=None):
    """Fresh joint review, including created controllers whose pods lag behind."""
    created = created or {}
    ns = entries[0]["deployment"]["metadata"]["namespace"]
    cache = {}
    def read(path):
        if path not in cache:
            try:
                cache[path] = kget(path)
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
                cache[path] = error
        value = cache[path]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)
    pods = ROLLOUT_CAPACITY.items(read, "/api/v1/pods")
    planned = copy.deepcopy(entries)
    starting_headroom = {}
    for entry in planned:
        previous = created.get(entry["name"])
        if previous is None:
            continue
        current = read(f"/apis/apps/v1/namespaces/{ns}/deployments/{entry['name']}")
        if current.get("metadata", {}).get("uid") != previous.get("metadata", {}).get("uid") or current.get("spec") != previous.get("spec"):
            raise ValueError(entry["name"] + " changed during batch creation; review the remaining services again")
        owned, known = ROLLOUT_CAPACITY.owned_pods(current, pods, read, ns)
        if not known:
            raise ValueError(entry["name"] + " pod ownership is unavailable; remaining services were not created")
        scheduled = [p for p in owned if p.get("spec", {}).get("nodeName") and not p.get("metadata", {}).get("deletionTimestamp")]
        for pod in scheduled:
            spec = pod["spec"]
            host = spec["nodeName"]
            starting_headroom[host] = starting_headroom.get(host, 0) + max(0,
                PLACE._pod_memory(spec)[0] - PLACE._pod_request(spec, "memory")) / 1024**3
        # Keep all scheduled/terminating consumers reserved. Replace only this
        # controller's unassigned pods with synthetic placements of its deficit.
        pending_ids = {id(p) for p in owned if not p.get("spec", {}).get("nodeName") and not p.get("metadata", {}).get("deletionTimestamp")}
        pods = [p for p in pods if id(p) not in pending_ids]
        entry["deployment"] = current
        entry["replicas"] = max(0, int(current["spec"].get("replicas", 1)) - len(scheduled))
    nodes = copy.deepcopy(PLACE.get_nodes())
    for node in nodes:
        node["batch_starting_headroom_gb"] = starting_headroom.get(node["name"], 0)
    return BATCH_CAPACITY.plan(planned, ns, pods, nodes, claims,
                               get_app_settings()["thresholds"]["memory"]["critical"], read=read)


def _unique_volume_name(base, used):
    base = re.sub(r"[^a-z0-9-]", "-", base.lower()).strip("-")[:55] or "volume"
    candidate, suffix = base, 2
    while candidate in used:
        tail = f"-{suffix}"
        candidate = base[:63 - len(tail)].rstrip("-") + tail
        suffix += 1
    used.add(candidate)
    return candidate


def build_sidecar_deployment(cfg, current):
    HOSTACCESS.require_target(current, cfg.get("namespace") or DEFAULT_NS)
    """Add one container to an existing Deployment pod template.

    Kubernetes cannot modify a running Pod. Updating the controller template causes a
    reviewed rollout, so all containers in the workload restart together.
    """
    container_name = _dns_name(cfg.get("container_name") or cfg.get("name"), "container name")
    target = _dns_name(cfg.get("target_workload"), "existing workload")
    if current.get("metadata", {}).get("name") != target:
        raise ValueError("existing workload does not match the selected Deployment")
    if cfg.get("network_mode") == "host":
        raise ValueError("host networking cannot be enabled while joining an existing workload")

    updated = json.loads(json.dumps(current))
    pspec = updated.setdefault("spec", {}).setdefault("template", {}).setdefault("spec", {})
    containers = pspec.setdefault("containers", [])
    if any(c.get("name") == container_name for c in containers):
        raise ValueError(f"container {container_name} already exists in {target}")

    env = [{"name": k, "value": str(v)} for k, v in (cfg.get("env") or {}).items()]
    ports = [{"containerPort": int(p["container"]),
              "name": (p.get("name") or f"p{p['container']}-{str(p.get('protocol', 'TCP')).lower()}")[:15],
              "protocol": str(p.get("protocol", "TCP")).upper()}
             for p in cfg.get("ports") or [] if p.get("container")]
    container = {"name": container_name, "image": UPDATES.with_tag(cfg["image"]), "imagePullPolicy": "IfNotPresent"}
    if env:
        container["env"] = env
    if ports:
        container["ports"] = ports
    resources = {}
    if cfg.get("cpu"):
        resources.setdefault("requests", {})["cpu"] = cfg["cpu"]
    memory_request = str(cfg.get("memory") or "").strip()
    memory_limit = str(cfg.get("memory_limit") or "").strip()
    MEMORY.validate(memory_request, memory_limit, container_name)
    if memory_request:
        resources.setdefault("requests", {})["memory"] = memory_request
    if memory_limit:
        resources.setdefault("limits", {})["memory"] = memory_limit
    if resources:
        container["resources"] = resources
    if cfg.get("privileged"):
        container["securityContext"] = {"privileged": True}

    pod_volumes = pspec.setdefault("volumes", [])
    existing_by_name = {v.get("name"): v for v in pod_volumes}
    used = set(existing_by_name)
    mounts = []
    for index, item in enumerate(cfg.get("volumes") or []):
        mount_path = (item.get("path") or "").strip()
        if not mount_path:
            continue
        if item.get("type") == "pod":
            volume_name = item.get("source", "")
            if volume_name not in existing_by_name:
                raise ValueError(f"pod volume {volume_name or '(blank)'} does not exist in {target}")
        else:
            source = (item.get("source") or "").strip()
            if not source and item.get("type") != "emptyDir":
                raise ValueError(f"storage source is required for {mount_path}")
            volume_name = ""
            if item.get("type") not in ("host", "emptyDir"):
                volume_name = next((v.get("name") for v in pod_volumes
                                    if v.get("persistentVolumeClaim", {}).get("claimName") == source), "")
            if not volume_name:
                volume_name = _unique_volume_name(f"hs-{container_name}-{index + 1}", used)
                if item.get("type") == "host":
                    pod_volumes.append({"name": volume_name, "hostPath": {"path": source}})
                elif item.get("type") == "emptyDir":
                    pod_volumes.append({"name": volume_name, "emptyDir": {}})
                else:
                    pod_volumes.append({"name": volume_name,
                                        "persistentVolumeClaim": {"claimName": source}})
        mount = {"name": volume_name, "mountPath": mount_path}
        if item.get("sub_path") and item.get("type") != "emptyDir":
            mount["subPath"] = str(item["sub_path"]).strip("/")
        if item.get("read_only"):
            mount["readOnly"] = True
        mounts.append(mount)

    hardware = set(cfg.get("hardware") or [])
    if cfg.get("gpu"):
        hardware.add("igpu")
    devices = {feature["id"]: HW.mount_spec(feature) for feature in HW.features()}
    unknown = hardware - set(devices)
    if unknown:
        raise ValueError("unknown hardware feature(s): " + ", ".join(sorted(unknown)))
    for feature_id in hardware:
        device = devices[feature_id]
        pspec.setdefault("nodeSelector", {})[device["label"]] = "true"
        container.setdefault("securityContext", {})["privileged"] = True
        volume_name = next((v.get("name") for v in pod_volumes
                            if v.get("hostPath", {}).get("path") == device["host_path"]), "")
        if not volume_name:
            volume_name = _unique_volume_name(device["name"], used)
            pod_volumes.append({"name": volume_name, "hostPath": {
                "path": device["host_path"], "type": device["path_type"]}})
        mounts.append({"name": volume_name, "mountPath": device["container_path"]})
    if mounts:
        container["volumeMounts"] = mounts
    containers.append(container)

    selector = current.get("spec", {}).get("selector", {}).get("matchLabels", {})
    service_cfg = dict(cfg)
    service_cfg["name"] = container_name
    # Only the Service portion is needed here. Pod storage and hardware have
    # already been merged above and pod-volume references are join-only.
    service_cfg["volumes"] = []
    service_cfg["hardware"] = []
    service_cfg["gpu"] = False
    _, service = build_deployment(service_cfg)
    if service:
        service["spec"]["selector"] = selector
    annotation_name = ("sidecar-" + container_name)[:63].rstrip("-")
    updated.setdefault("metadata", {}).setdefault("annotations", {})[
        NAMES.key(annotation_name)] = cfg.get("image", "")
    return updated, service


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
