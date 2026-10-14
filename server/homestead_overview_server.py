"""The overview, alerts, diagnostics and uptime code that lived in server.py
(track 1, part 1): the dashboard sampler, the alert sources and their loop,
uptime and schedule settings, the change-undo plan and the portal's status,
with the routes server.py's handler answered for them.

server.py binds what it still owns through bind_routes(): each helper in
ROUTE_NEEDS is looked up in server.py when it is called, so a test that
patches server.kget patches it here too; each name in VALUES is server.py's
own value (the same object, for _cache)."""
import copy
import json
import time
import urllib.request, urllib.parse, urllib.error
import homestead_alerts as ALERTS
import homestead_auth as AUTH
import homestead_changes as CHANGES
import homestead_cluster as CLUSTER
import homestead_diagnostics as DIAGNOSTICS
import homestead_disks as DISKS
import homestead_forecast as FORECAST
import homestead_history as HISTORY
import homestead_host_os as HOST_OS
import homestead_housekeeping as HOUSEKEEPING
import homestead_leader as LEADER
import homestead_lhcapacity as LHCAP
import homestead_lhrebuild as LHREBUILD
import homestead_mcp as MCP
import homestead_names as NAMES
import homestead_networking as NETWORK
import homestead_nodes as NODES
import homestead_operations as OPS
import homestead_outage as OUTAGE
import homestead_place as PLACE
import homestead_portal as PORTAL
import homestead_ports as PORTS
import homestead_push as PUSH
import homestead_recovery as RECOVERY
import homestead_resources as RESOURCES
import homestead_rollout_capacity as ROLLOUT_CAPACITY
import homestead_root_guard as ROOT_GUARD
import homestead_routes as MODULE_ROUTES
import homestead_schedules as SCHEDULES
import homestead_updates as UPDATES
import homestead_upgrades as UPGRADES
import homestead_uptime as UPTIME
import homestead_vips as VIPS
import homestead_vms as VMS
import homestead_vmusage as VMUSAGE
import homestead_workloads as WORKLOADS

ROUTE_NEEDS = ("_dns_name", "api_path", "beat", "cached", "get_app_settings", "get_overview", "guard_managed_smb", "is_self", "kget", "ksend", "outage_tick", "rollout_review_context", "schedule_act", "scheduled_items", "self_data_activity")
VALUES = ("API", "CTX", "TOKEN", "_cache")


def bind_routes(helpers, **values):
    missing = set(ROUTE_NEEDS) - set(helpers)
    if missing:
        raise TypeError("homestead_overview_server needs " + ", ".join(sorted(missing)))
    if set(values) != set(VALUES):
        raise TypeError("homestead_overview_server is bound with " + ", ".join(sorted(set(values) ^ set(VALUES))))
    globals().update({name: helpers[name] for name in ROUTE_NEEDS})
    globals().update(values)


def _sampler():
    while True:
        started = time.monotonic()
        try:
            o = get_overview()
            if LEADER.is_leader():
                HISTORY.record_live(o)
            # Each VM's disk traffic is a count, and a rate needs two readings.
            try:
                VMUSAGE.sample()
            except Exception:
                pass
            beat("sampler", 30, leader_only=True)
        except Exception as error:
            beat("sampler", 30, error, leader_only=True)
        time.sleep(max(1, 30 - (time.monotonic() - started)))


def _uptime_loop():
    """Every minute on the leader: does each running app answer at its address."""
    while True:
        started = time.time()
        if LEADER.is_leader():
            try:
                UPTIME.observe(cached("wl", 5, WORKLOADS.get_workloads), vms=cached("vms", 5, VMS.list_vms))
                try:
                    outage_tick()
                except Exception as error:
                    print(f"outage actions: {str(error)[:160]}", flush=True)
                beat("uptime", UPTIME.CHECK_EVERY, leader_only=True)
            except Exception as error:
                beat("uptime", UPTIME.CHECK_EVERY, error, leader_only=True)
                print(f"uptime: {str(error)[:160]}", flush=True)
        time.sleep(max(5, UPTIME.CHECK_EVERY - (time.time() - started)))


def change_undo_plan(body):
    """Undo one change: the app's settings from before it, reviewed like any rollout."""
    ns, name = _dns_name(body.get("ns"), "namespace"), _dns_name(body.get("name"), "workload name")
    guard_managed_smb(ns, name)
    if is_self(ns, name):
        raise ValueError("Homestead's own settings are changed from Settings, not undone here")
    entry = CHANGES.kept_before(ns, name, str(body.get("id") or ""))
    current = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    proposed, rows = CHANGES.undo_plan(current, entry)
    missing = sorted({(v.get("persistentVolumeClaim") or {}).get("claimName")
                      for v in proposed["spec"]["template"]["spec"].get("volumes") or []
                      if (v.get("persistentVolumeClaim") or {}).get("claimName")}
                     - {p["metadata"]["name"] for p in kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims").get("items", [])})
    if missing:
        raise ValueError(f"those settings used {', '.join(missing)}, which no longer exist{'s' if len(missing) == 1 else ''}")
    context = {"action": "change-undo", **rollout_review_context(current), "proposed_spec": proposed["spec"], "entry": entry["id"]}
    cache = {f"/apis/apps/v1/namespaces/{ns}/deployments/{name}": current}

    def read(path):
        if path not in cache:
            cache[path] = kget(path)
        return copy.deepcopy(cache[path])

    def planner(*args, **kwargs):
        return PLACE.manifest_plan(*args, **kwargs, read=read)
    capacity = ROLLOUT_CAPACITY.plan(current, proposed, ns, read, PLACE.get_nodes, planner,
                                     get_app_settings()["thresholds"]["memory"]["critical"])
    capacity["requires_confirmation"] = True
    return current, proposed, rows, capacity, context


def set_vm_monitoring(b):
    """How a VM is monitored: automatic, a port, a web page on a port, or off.
    An annotation on the VM, so it does not restart."""
    ns, name = _dns_name(b.get("ns"), "namespace"), _dns_name(b.get("name"), "VM name")
    mode = b.get("mode")
    # Automatic on a VM tries the usual ports; a port belongs to the other two.
    value = UPTIME.check_setting(mode, b.get("path"), b.get("port") if mode in ("tcp", "http") else None)
    if value in ("tcp",) or (value and value.startswith("/")):
        raise ValueError("choose the port to ask on this VM")
    ksend("PATCH", f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}",
          {"metadata": {"annotations": {NAMES.key("uptime"): value or None}}}, ctype="application/merge-patch+json")
    _cache.pop("vms", None)
    words = {"": "checked automatically", "off": "not monitored"}
    return {"ok": True, "detail": f"{name} is {words.get(value, f'checked at {value}')}"}


def _schedules_loop():
    """Every minute on the leader: any scheduled stop or start that is due.
    Its writes are noted as the schedule's, so change history leaves them out."""
    CHANGES.REQUEST.user = CHANGES.BY_SCHEDULE
    while True:
        started = time.time()
        if LEADER.is_leader():
            try:
                if SCHEDULES.tick(scheduled_items(), schedule_act):
                    _cache.pop("wl", None)
                    _cache.pop("vms", None)
                beat("schedules", 60, leader_only=True)
            except Exception as error:
                beat("schedules", 60, error, leader_only=True)
                print(f"schedules: {str(error)[:160]}", flush=True)
        time.sleep(max(5, 60 - (time.time() - started)))


def set_uptime_setting(b):
    """An app's own check: auto, an HTTP path, TCP only, or off - on any of its
    published ports, or one chosen. An annotation, so nothing restarts."""
    ns, name = _dns_name(b.get("ns"), "namespace"), _dns_name(b.get("name"), "workload name")
    port = b.get("port") if b.get("mode") != "off" else None
    if port not in (None, ""):
        w = next((w for w in cached("wl", 5, WORKLOADS.get_workloads) if w["ns"] == ns and w["name"] == name), None)
        published = {int(p["port"]) for p in (w or {}).get("ports") or [] if p.get("port")}
        if w is not None and str(port).isdigit() and int(port) not in published:
            raise ValueError(f"{name} does not publish port {port}; choose one of "
                             + (", ".join(str(p) for p in sorted(published)) or "its ports"))
    value = UPTIME.check_setting(b.get("mode"), b.get("path"), port)
    ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}",
          {"metadata": {"annotations": {NAMES.key("uptime"): value or None}}}, ctype="application/merge-patch+json")
    _cache.pop("wl", None)
    return {"ok": True, "value": value, "detail": f"{name} is {UPTIME.describe_setting(value)}"}


def portal_status(force=False):
    """Each portal link's dot. A link to an app takes the app's own Monitoring
    state - asked every minute, over HTTP, down only after three misses - so
    the portal and Containers agree; any other link is a TCP connection."""
    status = dict(PORTAL.status(force=force))
    try:
        answers, links = UPTIME.report(), PORTAL.stored()
    except Exception:
        return status
    for link in links:
        icon = str(link.get("icon") or "")
        if not icon.startswith("workload:"):
            continue
        a = answers.get(icon.split(":", 1)[1])
        if not a or a.get("state") not in ("up", "slow", "down"):
            continue
        last = a.get("last") or {}
        status[link["id"]] = {"up": a["state"] != "down", "ms": last.get("ms"), "state": a["state"], "source": "monitoring",
                              "since": a.get("since"), "error": last.get("error") or "", "code": last.get("code")}
    return status


def _diagnostic_read(path, timeout=5):
    req = urllib.request.Request(API + api_path(path), headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, context=CTX, timeout=timeout) as response:
        raw = response.read(DIAGNOSTICS.MAX_SOURCE_BYTES + 1)
    if "/log?" in path:
        return raw.decode("utf-8", "replace")
    return json.loads(raw)


# ------------------------------------------------------------- alerts
UPDATE_SCAN_EVERY = UPDATES.FRESH_FOR
_last_update_scan = [0.0]


def _alert_sources():
    """What each source sees now; None where it could not look."""
    results = {}

    def take(name, fn):
        try:
            results[name] = fn()
        except Exception:
            results[name] = None

    take("health", lambda: ALERTS.health_facts(cached("ov", 10, get_overview)))
    take("jobs", lambda: ALERTS.job_facts(OPS.list_operations()))
    take("joins", lambda: ALERTS.join_facts(kget("/api/v1/nodes").get("items", [])))
    take("addresses", lambda: VIPS.alert_facts(cached("network", 5, NETWORK.inventory).get("addresses")))
    take("capacity", lambda: LHCAP.alert_facts(cached("lhcap", 15, LHCAP.status)))
    take("detached-copies", lambda: LHREBUILD.alert_facts(cached("lhrebuild", 30, LHREBUILD.status)))
    take("disks", lambda: DISKS.alert_facts(cached("disks", 15, DISKS.inventory)))
    take("hostos", HOST_OS.alert_facts)
    take("ports", lambda: PORTS.alert_facts(cached("ports", 20, NODES.ports_report)))
    take("uptime", lambda: UPTIME.alert_facts(UPTIME.report()))
    take("schedules", lambda: SCHEDULES.alert_facts())
    take("outage", lambda: OUTAGE.alert_facts())
    take("housekeeping", lambda: HOUSEKEEPING.alert_facts(cached("housekeeping", 60, HOUSEKEEPING.report)))
    take("forecast", lambda: FORECAST.alert_facts(FORECAST.report()))
    take("recovery", RECOVERY.alert_facts)
    take("agents", MCP.alert_facts)
    take("rootguard", ROOT_GUARD.alert_facts)
    take("platform", lambda: ALERTS.upgrade_facts(UPGRADES.report(
        ((cached("cluster", 15, CLUSTER.inventory) or {}).get("versions") or {}).get("harvester", ""))))
    # Twice a day whether or not anyone is looking - the Containers header and
    # the update notifications both read the result.
    if time.time() - _last_update_scan[0] > UPDATE_SCAN_EVERY:
        _last_update_scan[0] = time.time()
        try:
            UPDATES.report()
        except Exception:
            pass
    latest = UPDATES._LATEST.get("report")
    results["updates"] = ALERTS.update_facts(latest) if latest else None
    return results


def push_alerts(fresh):
    """Wakes every device that wants one of these alerts, and belongs to a user still here."""
    if not fresh:
        return None
    users = {u["name"] for u in AUTH.list_users()}
    urgent = any(e["severity"] == "critical" and e["phase"] in ("raised", "worsened") for e in fresh)
    visible = {user: ALERTS.for_user(fresh, user) for user in users}
    return PUSH.send(lambda row: row["user"] in users and any(
                         e["category"] in row["categories"] for e in visible[row["user"]]),
                     urgency="high" if urgent else "normal")


def _alerts_loop():
    while True:
        # One replica raises alerts, or every notification arrives twice.
        if LEADER.is_leader():
            try:
                with self_data_activity():
                    push_alerts(ALERTS.observe(_alert_sources()))
                beat("alerts", 20, leader_only=True)
            except Exception as error:
                beat("alerts", 20, error, leader_only=True)
                print(f"alerts: {str(error)[:160]}", flush=True)
        time.sleep(20)


# The routes server.py's handler answered for this code.
def _portal_status(request):
    q = request.query
    return portal_status(force=(q.get("force") or [""])[0] == "1")


def _resource(request):
    q, p = request.query, request.path
    arg = lambda key: (q.get(key) or [""])[0]
    if p == "/api/resources/list":
        return RESOURCES.list_objects(arg("group"), arg("version"), arg("resource"), arg("ns"))
    return RESOURCES.get_object(arg("group"), arg("version"), arg("resource"), arg("ns"),
                                arg("name"), reveal=p.endswith("reveal"))


def _push_test(request):
    b = request.body
    endpoint = str(b.get("endpoint") or "")
    if not PUSH.mine(request.user, endpoint):
        return MODULE_ROUTES.Reply(404, {"error": "this device is not set up for notifications"})
    ALERTS.note({"key": f"test:{int(time.time())}", "category": "test", "severity": "info",
                 "title": "Homestead test notification",
                 "body": "Notifications are enabled for this device. Choose categories in Settings › This device.",
                 "href": "/settings", "to": PUSH.tag(endpoint)})
    result = PUSH.send(lambda row: row["endpoint"] == endpoint, urgency="high")
    status = (result["statuses"] or [0])[0]
    if not result["sent"]:
        return MODULE_ROUTES.Reply(502, {"error": f"the push service refused the push (HTTP {status})"
                                         if status else "the push service could not be reached"})
    return {"ok": True}


def _alerts_acknowledge(request):
    b = request.body
    try:
        return ALERTS.acknowledge(request.user, b.get("key"), b.get("version"), b.get("undo") is True)
    except ALERTS.AlertChanged as error:
        return MODULE_ROUTES.Reply(409, {"error": str(error)})


def _alerts_delivered(request):
    b = request.body
    endpoint, cursor = str(b.get("endpoint") or ""), b.get("latest")
    if not PUSH.mine(request.user, endpoint):
        return MODULE_ROUTES.Reply(404, {"error": "This notification device is not registered to your account."})
    if type(cursor) is not int or cursor < 0 or cursor > ALERTS.log(limit=0)["latest"]:
        return MODULE_ROUTES.Reply(400, {"error": "Invalid notification cursor."})
    PUSH.advance(request.user, endpoint, cursor)
    return {"ok": True}


ROUTES = {
    ("GET", "/api/portal/status"): ("viewer", _portal_status),
    ("GET", "/api/resources/list"): ("viewer", _resource),
    ("GET", "/api/resources/object"): ("viewer", _resource),
    ("GET", "/api/resources/reveal"): ("admin", _resource),
    ("POST", "/api/push/test"): ("viewer", _push_test),
    ("POST", "/api/alerts/acknowledge"): ("viewer", _alerts_acknowledge),
    ("POST", "/api/alerts/delivered"): ("viewer", _alerts_delivered),
}
