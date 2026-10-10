"""Containers: the list, an app's edit form, its logs, and what is done to one
from its card - scale, restart, edit, move to another host.

server.py used to answer all of this in place (#363). The routes are declared
here now; the helpers they still share with server.py's other code are bound
by name (bind), so a test that replaces one in server.py replaces it here too.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

import homestead_capacity_review as CAPACITY_REVIEW
import homestead_copy_job as COPY_JOB
import homestead_failover as FAILOVER
import homestead_logsearch as LOGSEARCH
import homestead_names as NAMES
import homestead_operations as OPS
import homestead_place as PLACE
import homestead_rename as RENAME
import homestead_restructure as RESTRUCTURE
import homestead_rollout_capacity as ROLLOUT_CAPACITY
import homestead_routes as ROUTER

# server.py's helpers these routes call, bound by name at start.
NEEDS = ("kget", "ksend", "get_workloads", "workload_edit_payload", "workload_start_plan",
         "scale_workload", "restart_workload", "set_workload_groups", "set_workload_logos",
         "guard_managed_smb", "guard_self", "_dns_name", "get_app_settings", "edit_capacity_plan",
         "apply_reviewed_edit", "preview_host_move", "reviewed_host_move", "logsearch_read")
# And its values: the API server, the cache, where apps go by default.
API = TOKEN = CTX = None
DEFAULT_NS = "lab"
_cache = {}


def bind(helpers, *, api, token, ssl_context, cache, default_ns):
    global API, TOKEN, CTX, _cache, DEFAULT_NS
    missing = set(NEEDS) - set(helpers)
    if missing:
        raise TypeError("homestead_workloads needs " + ", ".join(sorted(missing)))
    globals().update({name: helpers[name] for name in NEEDS})
    API, TOKEN, CTX, _cache, DEFAULT_NS = api, token, ssl_context, cache, default_ns


class WorkloadRefused(Exception):
    """A start the capacity check will not allow as asked: 409, with its plan."""
    def __init__(self, message, plan):
        super().__init__(message)
        self.plan = plan


def _arg(request, key, default=""):
    return (request.query.get(key) or [default])[0]


def logs_refusal(error, pod, container):
    """What Kubernetes' refusal to give logs means, in words rather than its JSON."""
    try:
        message = json.loads(error.read().decode("utf-8", "replace")).get("message", "")
    except Exception:
        message = ""
    who = f"{container} in {pod}" if container else pod
    if "waiting to start" in message or "ContainerCreating" in message or "PodInitializing" in message:
        return f"{who} has not started yet, so there are no logs. It shows here once it is running."
    if "not found" in message and "pods" in message:
        return f"{pod} no longer exists; the workload has probably replaced it. Reopen the logs."
    if "terminated" in message:
        return f"{who} has stopped and Kubernetes kept no logs from it."
    return f"Kubernetes could not return the logs for {who}" + (f": {message}" if message else ".")


def logs(request):
    """A pod's last lines, as text. A job's logs are its pod's."""
    q = request.query
    ns = q["ns"][0]
    pod = _arg(request, "pod")
    job = _arg(request, "job")
    if job and not pod:
        matches = kget(f"/api/v1/namespaces/{ns}/pods?labelSelector=job-name%3D{urllib.parse.quote(job)}").get("items", [])
        pod = matches[0]["metadata"]["name"] if matches else ""
    if not pod:
        return ROUTER.Reply(404, {"error": "Logs are not available yet because no pod exists."})
    tail = max(20, min(1000, int((q.get("tail") or [300])[0])))
    container = _arg(request, "container")
    if not container:
        # A pod with more than one container has to be told which;
        # asking without a name is a 400 from Kubernetes.
        try:
            spec = kget(f"/api/v1/namespaces/{ns}/pods/{pod}").get("spec", {}) or {}
            names = [c.get("name") for c in spec.get("containers", []) or []]
            container = names[0] if names else ""
        except Exception:
            container = ""
    query = f"tailLines={tail}&timestamps=true" + (
        f"&container={urllib.parse.quote(container)}" if container else "")
    req = urllib.request.Request(f"{API}/api/v1/namespaces/{ns}/pods/{pod}/log?{query}",
                                 headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=15) as r:
            return ROUTER.Reply(200, r.read().decode("utf-8", "replace"), "text/plain; charset=utf-8")
    except urllib.error.HTTPError as error:
        return ROUTER.Reply(409 if error.code in (400, 404) else error.code,
                            {"error": logs_refusal(error, pod, container)})


def search_logs(request):
    apps = [a for a in _arg(request, "apps").split(",") if a]
    return LOGSEARCH.search(
        ROUTER.cached("wl", 5, get_workloads), _arg(request, "q"), logsearch_read,
        window=_arg(request, "window", "1h"), apps=apps,
        regex=_arg(request, "regex") == "1", case=_arg(request, "case") == "1")


def edit_form(request):
    ns, name = request.query["ns"][0], request.query["name"][0]
    deployment = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    return workload_edit_payload(ns, name, deployment)


def scale(request):
    b = request.body
    try:
        scale_workload(b["ns"], b["name"], int(b["replicas"]),
                       confirm_capacity=b.get("confirm_capacity") is True,
                       confirm_self=b.get("confirm_self") is True)
    except WorkloadRefused as refused:
        return ROUTER.Reply(409, {"error": str(refused), "plan": refused.plan})
    return {"ok": True}


def set_failover(request):
    b = request.body
    for item in b.get("items") or []:
        guard_managed_smb(item.get("ns"), item.get("name"))
    result = FAILOVER.set_many(b.get("items") or [])
    _cache.pop("wl", None)
    return result


def set_primary_port(request):
    b = request.body
    ns, name = b.get("ns") or DEFAULT_NS, _dns_name(b.get("name"), "workload name")
    guard_managed_smb(ns, name)
    port = int(b.get("port") or 0)
    ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}",
          {"metadata": {"annotations": {NAMES.key("primary-port"): str(port) if port else None}}},
          ctype="application/merge-patch+json")
    _cache.pop("wl", None)
    return {"ok": True, "detail": f"{name} opens on port {port} first" if port
            else f"{name} shows its ports in their own order"}


def preview_edit(request):
    b = request.body
    guard_managed_smb(b.get("ns", ""), b.get("name", ""))
    _, context, plan = edit_capacity_plan(b)
    return {"capacity": plan, "capacity_token": CAPACITY_REVIEW.issue(b, context)}


def edit(request):
    b = request.body
    guard_managed_smb(b.get("ns", ""), b.get("name", ""))
    prepared, context, plan = edit_capacity_plan(b)
    CAPACITY_REVIEW.enforce(b, plan, context)
    if context.get("action") == RENAME.KIND:
        def admission(dep):
            return PLACE.manifest_plan(dep, b["ns"], dep["metadata"]["name"], dep["spec"]["replicas"],
                get_app_settings()["thresholds"]["memory"]["critical"], read=kget,
                nodes_snapshot=PLACE.get_nodes(), pod_snapshot=ROLLOUT_CAPACITY.items(kget, "/api/v1/pods"))
        result = RENAME.dispatch(b, context, prepared["deployment"], kget, ksend, OPS, admission)
        _cache.pop("wl", None); _cache.pop("ov", None); _cache.pop("network", None)
        return result
    moves = RESTRUCTURE.copies(b)
    guard_self(b.get("ns", ""), b.get("name", ""),
               stopping=("autostart" in b and not b["autostart"]) or
                        ("replicas" in b and int(b.get("replicas") or 0) == 0),
               confirmed=b.get("confirm_self") is True, moving=bool(moves))
    if moves:
        return COPY_JOB.start(b, context, prepared, moves, kget, OPS,
                              lambda: apply_reviewed_edit(b, prepared, hold=True))
    return apply_reviewed_edit(b, prepared)


def move_to_host(request):
    """Another host for a container, after its capacity review."""
    b = request.body
    result = reviewed_host_move(b)
    result["operation"] = OPS.start(
        "deployment", f"Move {b['name']}",
        {"kind": "Deployment", "name": b["name"], "namespace": b["ns"]},
        "/containers", {"namespace": b["ns"], "name": b["name"], "undo": "rollout"})
    return result


# Its routes and who may use them (homestead_routes.py). server.py checks
# /api/move's target is an app before it gets here.
ROUTES = {
    ("GET", "/api/workloads"): ("viewer", lambda request: ROUTER.cached("wl", 5, get_workloads)),
    ("GET", "/api/workloads/start-plan"): ("viewer", lambda request: workload_start_plan(
        _arg(request, "ns"), _arg(request, "name"), int((request.query.get("replicas") or [1])[0]))),
    ("GET", "/api/workload"): ("viewer", edit_form),
    ("GET", "/api/logs"): ("viewer", logs),
    ("GET", "/api/logs/search"): ("viewer", search_logs),
    ("POST", "/api/workloads/group"): ("operator", lambda request: set_workload_groups(request.body)),
    ("POST", "/api/workloads/logo"): ("operator", lambda request: set_workload_logos(request.body)),
    ("POST", "/api/workloads/failover"): ("operator", set_failover),
    ("POST", "/api/workload/primary-port"): ("operator", set_primary_port),
    ("POST", "/api/scale"): ("operator", scale),
    ("POST", "/api/restart"): ("operator", lambda request: restart_workload(request.body["ns"], request.body["name"])),
    ("POST", "/api/edit/preview"): ("operator", preview_edit),
    ("POST", "/api/edit"): ("operator", edit),
    ("POST", "/api/move/preview"): ("operator", lambda request: preview_host_move(request.body)),
    ("POST", "/api/move"): ("operator", move_to_host),
}
