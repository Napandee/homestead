"""Priority tiers: which apps fit first when the cluster is short of room.

When a host fails, its apps start on the others; if those cannot hold them
all, the scheduler fits them in its own order, and the home automation can
wait behind a media scanner (#347). Kubernetes orders pods by their
PriorityClass, so Homestead keeps two of its own and calls having none a tier:

* critical (homestead-critical) - fits first, and may evict any pod of lower
  priority to fit: low apps, and every pod with no class - other apps and
  VMs. For what the house depends on. Homestead itself joins this class once
  anything uses it (protect), since pods of one priority never evict each
  other.
* normal - no class at all, as every app has now. On a cluster with a
  globalDefault class of its own, "no class" means that one.
* low (homestead-low) - waits, and never evicts anything (preemptionPolicy
  Never): batch work and CI runners.

Homestead makes neither the cluster's globalDefault, which would apply to
every new pod - system pods included - and makes each only when it is first
used. An app's tier is read from its pod spec, so a class set by hand or by
Flux is shown as it is; a class Homestead does not keep is shown by name and
left alone. homestead-normal - a class #347's first plan suggested making by
hand - still reads as normal.

VMs have tiers too, set in their template: a VM takes its tier when it next
starts, so setting one restarts nothing.

Changing a tier changes the pod template, so the app restarts once. Like the
reservations (homestead_requests), the tiers are reviewed as a list and
applied by a job one app at a time; Flux- and Argo-applied apps get the line
to commit instead. Homestead's own tier is not offered: it is critical once
anything is (protect), and has no class before. Neither its manifest nor an
update names a class, and an apply keeps a field it does not name.
"""
import hashlib
import json
import re
import time
import urllib.error

KIND = "set-priority"
MAX_APPS = 50
SYSTEM = ("kube-", "cattle-", "harvester-", "longhorn-", "fleet-")
VMS = "/apis/kubevirt.io/v1/virtualmachines"
TIERS = {
    "critical": {"name": "homestead-critical", "value": 1000, "preemptionPolicy": "PreemptLowerPriority",
                 "description": "Homestead: fits first, and may evict lower-priority pods to fit."},
    "normal": {"name": None},
    "low": {"name": "homestead-low", "value": -1000, "preemptionPolicy": "Never",
            "description": "Homestead: waits for room, and never evicts anything."},
}
BY_CLASS = {"homestead-critical": "critical", "homestead-normal": "normal", "homestead-low": "low"}
# What an app is, from its name or image, when it is likely critical or low.
# A suggestion only: it is ticked in the review, never applied unseen.
HINTS = [
    ("critical", re.compile(r"home-?assistant|zigbee2mqtt|z-?wave|mosquitto|vaultwarden|bitwarden|"
                            r"postgres|mariadb|mysql|cloudflared|tailscale|wireguard|pi-?hole|adguard")),
    ("low", re.compile(r"actions-runner|gitlab-runner|renovate|\brunner\b")),
]
# A helper beside one of those - its exporter, backup or sync - is not it.
HELPER = re.compile(r"(exporter|backup|sync|metrics|dashboard|\bui|admin)\b")
WHY = {"critical": "looks like home automation, a password manager, a database, DNS or a tunnel",
       "low": "looks like batch or CI work"}

kget = ksend = None
managed_by = lambda dep: {}
own = ("", "")
system_ns = frozenset()
is_leader = lambda: True
guard = lambda dep, ns, role: None   # as in homestead_requests: may this role change it
operations = None                    # homestead_operations: the job is one of its jobs


def bind(_kget, _ksend, _managed_by, _own, _system_ns, _is_leader=None, _guard=None, _operations=None):
    global kget, ksend, managed_by, own, system_ns, is_leader, guard, operations
    kget, ksend, managed_by, own, system_ns = _kget, _ksend, _managed_by, tuple(_own), frozenset(_system_ns)
    operations = _operations
    if operations:
        # Its jobs resume and cancel through the job list like every other.
        operations.RESOLVERS[KIND] = status
        operations.CANCELLERS[KIND] = (cancel_plan, cancel_run)
    if _is_leader:
        is_leader = _is_leader
    if _guard:
        guard = _guard


def tier_of(pod_spec):
    """(tier, class name): normal for no class; "other" for one Homestead
    does not keep, which is left alone."""
    name = (pod_spec or {}).get("priorityClassName") or ""
    if not name:
        return "normal", ""
    return BY_CLASS.get(name, "other"), name


def suggestion(dep):
    """The tier an app's name or image suggests, and why; None for neither."""
    meta = dep.get("metadata") or {}
    images = " ".join(c.get("image", "") for c in (((dep.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or [])
    text = f"{meta.get('name', '')} {images}".lower()
    if HELPER.search(meta.get("name", "").lower()):
        return None, ""
    for tier, pattern in HINTS:
        if pattern.search(text):
            return tier, WHY[tier]
    return None, ""


NO_PERMISSION = ("Homestead may not make priority classes yet: its permissions predate this version. "
                 "Settings → Homestead → About this installation → Permissions says how to bring them up to date.")


def ensure(tiers=("critical", "low")):
    """The classes these tiers use, made where missing. One that exists with
    another value or policy is kept as it is - someone made it - and said so."""
    notes = []
    for tier in tiers:
        spec = TIERS[tier]
        if not spec["name"]:
            continue
        try:
            found = kget(f"/apis/scheduling.k8s.io/v1/priorityclasses/{spec['name']}")
            if (found.get("value"), found.get("preemptionPolicy", "PreemptLowerPriority")) != (spec["value"], spec["preemptionPolicy"]):
                notes.append(f"{spec['name']} exists with value {found.get('value')} and "
                             f"{found.get('preemptionPolicy', 'PreemptLowerPriority')}; it is kept")
            continue
        except urllib.error.HTTPError as error:
            if error.code == 403:
                raise PermissionError(NO_PERMISSION)
            if error.code != 404:
                raise
        try:
            ksend("POST", "/apis/scheduling.k8s.io/v1/priorityclasses", {
                "apiVersion": "scheduling.k8s.io/v1", "kind": "PriorityClass",
                "metadata": {"name": spec["name"], "labels": {"app.kubernetes.io/managed-by": "homestead"}},
                "value": spec["value"], "preemptionPolicy": spec["preemptionPolicy"],
                "globalDefault": False, "description": spec["description"]})
        except urllib.error.HTTPError as error:
            if error.code == 403:
                raise PermissionError(NO_PERMISSION)
            # Made meanwhile by an edit or another job: there now, as wanted.
            if error.code != 409:
                raise
    return notes


def _workloads():
    """("app", Deployment) and ("vm", VirtualMachine) outside the platform's
    namespaces; no VMs where KubeVirt is not installed."""
    try:
        vms = kget(VMS).get("items", [])
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        vms = []
    for kind, obj in [("app", d) for d in kget("/apis/apps/v1/deployments").get("items", [])] + [("vm", v) for v in vms]:
        ns = (obj.get("metadata") or {}).get("namespace", "")
        if ns not in system_ns and not ns.startswith(SYSTEM):
            yield kind, obj


def _row_id(kind, ns, name):
    return f"vm:{ns}/{name}" if kind == "vm" else f"{ns}/{name}"


def _split(row_id):
    """(kind, ns, name) from a row's id: a VM's starts vm:."""
    kind = "vm" if row_id.startswith("vm:") else "app"
    ns, name = row_id[3:].split("/", 1) if kind == "vm" else row_id.split("/", 1)
    return kind, ns, name


def _path(kind, ns, name):
    return (f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}" if kind == "vm"
            else f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")


def _pod_spec(dep):
    return ((dep.get("spec") or {}).get("template") or {}).get("spec") or {}


def plan(chosen=None, role=None):
    """Every app with its tier now, the tier suggested, and the tier it is
    set to in the review: by default the suggestion for an app still at
    normal, else what it has. chosen maps an app id to a tier. role: who
    asks; apps they may not change are left out, saying why."""
    apps, skipped = [], []
    for kind, dep in _workloads():
        meta = dep.get("metadata") or {}
        ns, name = meta.get("namespace", ""), meta.get("name", "")
        app_id = _row_id(kind, ns, name)
        current, cls = tier_of(_pod_spec(dep))
        suggested, why = suggestion(dep)
        if kind == "app" and (ns, name) == own:
            skipped.append({"id": app_id, "why": "Homestead's own tier is critical once any app's is, so none can evict it"})
            continue
        if current == "other":
            skipped.append({"id": app_id, "why": f"it uses the class {cls}, which Homestead does not keep; it is left alone"})
            continue
        try:
            guard(dep, ns, role)
        except PermissionError as error:
            skipped.append({"id": app_id, "why": str(error)})
            continue
        owner = managed_by(dep)
        row = {"id": app_id, "kind": kind, "ns": ns, "name": name, "current": current, "suggested": suggested or "", "why": why,
               "managed": owner.get("by", "")}
        if owner:
            where = f" ({owner['source']})" if owner.get("source") else ""
            row["note"] = (f"{owner['by']} applies it{where}; set priorityClassName there" if owner.get("detected")
                           else f"marked as updated by {owner['by']}; set priorityClassName there")
        default = suggested if suggested and current == "normal" and not owner else current
        row["tier"] = (chosen or {}).get(app_id, default) if not owner else current
        if row["tier"] not in TIERS:
            raise ValueError(f"{app_id}: the tier must be critical, normal or low")
        apps.append(row)
    # By the tier each has now, so a row stays where it is as a tier is picked.
    apps.sort(key=lambda a: ({"critical": 0, "normal": 1, "low": 2}[a["current"]], a["id"]))
    changes = [a for a in apps if a["tier"] != a["current"]]
    # The suggestions never start more than one run may change.
    if chosen is None and len(changes) > MAX_APPS:
        for app in changes[MAX_APPS:]:
            app["tier"] = app["current"]
        changes = changes[:MAX_APPS]
    return {"apps": apps, "skipped": sorted(skipped, key=lambda s: s["id"]), "max": MAX_APPS,
            "changes": len(changes), "review_token": review_token(changes)}


def review_token(changes):
    rows = sorted([c["id"], c["current"], c["tier"]] for c in changes)
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()[:20]


def reviewed(changes, token, role=None):
    """The tier changes a person reviewed, each app checked as it is now."""
    if not isinstance(changes, list) or not changes:
        raise ValueError("Change at least one app's tier")
    if len(changes) > MAX_APPS:
        raise ValueError(f"At most {MAX_APPS} apps at a time")
    if any(not isinstance(c, dict) or not isinstance(c.get("id"), str) or "/" not in c["id"]
           or c.get("current") not in TIERS or c.get("tier") not in TIERS or c["current"] == c["tier"] for c in changes):
        raise ValueError("Review the tiers again")
    if review_token(changes) != token:
        raise ValueError("The tiers differ from the review; review again")
    out = []
    for c in changes:
        kind, ns, name = _split(c["id"])
        check(ns, name, c["current"], role, kind=kind)
        out.append({"id": c["id"], "kind": kind, "ns": ns, "name": name, "current": c["current"], "tier": c["tier"]})
    return out


def check(ns, name, current, role=None, tier=None, kind="app"):
    """One app as it is now: still one Homestead - and the person who asked -
    may change, and still in the tier it was reviewed in. Returns "applied"
    when it already has the new tier: a change whose answer was lost is waited
    for, not counted as skipped."""
    app_id = _row_id(kind, ns, name)
    try:
        dep = kget(_path(kind, ns, name))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise ValueError(f"{app_id} is gone")
        raise
    if (kind == "app" and (ns, name) == own) or ns in system_ns or ns.startswith(SYSTEM) or managed_by(dep):
        raise ValueError(f"{app_id} is not one Homestead changes")
    guard(dep, ns, role)
    now = tier_of(_pod_spec(dep))[0]
    if tier and now == tier:
        return "applied"
    if now != current:
        raise ValueError(f"{app_id}'s tier changed since the review")
    return "ready"


def patch(app):
    """Normal is no class at all: the class is removed rather than set. What
    a template copied from a pod carries (priority, preemptionPolicy) goes
    too: admission refuses a pod whose values are not its class's."""
    cls = None if app["tier"] == "normal" else TIERS[app["tier"]]["name"]
    if app.get("kind") == "vm":
        # A VM's pod is made from its template when it starts: no restart now.
        ksend("PATCH", _path("vm", app["ns"], app["name"]), {"spec": {"template": {"spec": {"priorityClassName": cls}}}},
              ctype="application/merge-patch+json")
        return
    # preemptionPolicy too: admission refuses one that is not the class's,
    # and a low app's is Never.
    ksend("PATCH", f"/apis/apps/v1/namespaces/{app['ns']}/deployments/{app['name']}",
          {"spec": {"template": {"spec": {"priorityClassName": cls, "priority": None, "preemptionPolicy": None}}}},
          ctype="application/strategic-merge-patch+json")


# --------------------------------------------------------------- the job
def _settled(ns, name):
    dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    status, spec, meta = dep.get("status") or {}, dep.get("spec") or {}, dep.get("metadata") or {}
    want = int(spec.get("replicas") or 0)
    return (int(status.get("observedGeneration") or 0) >= int(meta.get("generation") or 0)
            and int(status.get("updatedReplicas") or 0) == int(status.get("readyReplicas") or 0) == int(status.get("replicas") or 0) == want)


def status(item):
    ref = item["ref"]
    apps, i, done = ref["apps"], ref.get("index", 0), ref.get("done", 0)
    if not ref.get("classes"):
        if not is_leader():
            return "running", 0, item.get("message") or "Making the priority classes"
        try:
            ref["notes"] = ensure(sorted({a["tier"] for a in apps}))
        except PermissionError as error:
            return "failed", 0, str(error)
        ref["classes"] = True
        return "running", 0, "Priority classes ready" + (f"; {'; '.join(ref['notes'])}" if ref["notes"] else "")
    if i >= len(apps):
        skipped = ref.get("skipped") or []
        return "succeeded", 100, (f"Set the tier of {done} app{'' if done == 1 else 's'}"
                                  + (f"; left {len(skipped)}: " + "; ".join(skipped[:4]) if skipped else ""))
    app = apps[i]
    progress = int(100 * i / max(1, len(apps)))
    if not is_leader():
        return "running", progress, item.get("message") or f"Setting {app['id']}"
    stage, now = ref.get("stage", "apply"), time.time()
    if stage == "apply":
        try:
            if check(app["ns"], app["name"], app["current"], ref.get("role"), app["tier"], app.get("kind", "app")) == "ready":
                patch(app)
        except (ValueError, PermissionError, urllib.error.HTTPError) as error:
            if isinstance(error, urllib.error.HTTPError) and error.code not in (403, 404, 409, 422):
                raise
            why = f"Kubernetes refused it ({error.code})" if isinstance(error, urllib.error.HTTPError) else str(error)
            ref.setdefault("skipped", []).append(f"{app['id']}: {why[:160]}")
            ref.update(index=i + 1)
            return "running", progress, f"Skipped {app['id']}: {why[:160]}"
        if app.get("kind") == "vm":
            ref.update(index=i + 1, done=done + 1)
            return "running", int(100 * (i + 1) / len(apps)), f"{app['name']} takes {app['tier']} when it next starts"
        ref.update(stage="rolling", started=now)
        return "running", progress, f"Setting {app['id']} to {app['tier']} ({i + 1} of {len(apps)}); it restarts"
    if stage == "rolling":
        if _settled(app["ns"], app["name"]):
            ref.update(index=i + 1, stage="apply", done=done + 1)
            return "running", int(100 * (i + 1) / len(apps)), f"{app['id']} runs as {app['tier']}"
        if now - ref.get("started", now) > 600:
            return "failed", progress, (f"{app['id']} did not come back within 10 minutes; the rest were not changed. "
                                        f"Set {done} before it. Check its events in Containers")
        return "running", progress, f"Waiting for {app['id']} to restart ({i + 1} of {len(apps)})"
    return "failed", progress, "Unknown step; the rest were not changed"


def cancel_plan(item):
    return {"mode": "stop", "action": "Stop setting tiers",
            "undo": ["No more apps are changed"],
            "keeps": ["An app already restarting finishes in its new tier",
                      "Apps already changed keep their new tier; the classes stay"], "needs": "operator"}


def cancel_run(work, chosen):
    done = (work.get("ref") or {}).get("done", 0)
    return f"Stopped after setting {done} app{'' if done == 1 else 's'}"


def start(body, role=None):
    """The job for the tiers a person reviewed, checked against the cluster
    now; role is theirs, and each app is checked for them again as it is
    changed. Refused while another such job runs, or without the restarts
    confirmed."""
    running = next((i for i in operations.list_operations() if i.get("kind") == KIND
                    and i.get("status") not in operations.TERMINAL), None)
    if running:
        raise ValueError("Tiers are already being set; follow it in Jobs")
    if (body or {}).get("restart") is not True:
        raise ValueError("A new tier restarts each app; confirm the restarts")
    apps = reviewed(body.get("changes"), body.get("review_token"), role)
    return {"operation": operations.start(KIND, f"Set the priority tier of {len(apps)} app{'' if len(apps) == 1 else 's'}",
                                          {"kind": "Deployment", "name": "priority"}, "/containers",
                                          {"apps": apps, "index": 0, "done": 0, "stage": "apply", "role": role},
                                          "Making the priority classes")}


# ------------------------------------------------- Homestead's own tier
def protect():
    """Homestead in homestead-critical once anything runs there, so no
    critical app can evict it - pods of one priority never evict each other.
    Not while a job runs, which its restart would interrupt; never over a
    class someone gave it. Returns a line for the log, or None."""
    ns, name = own
    if not ns or not name:
        return None
    dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    critical = TIERS["critical"]["name"]
    if _pod_spec(dep).get("priorityClassName"):
        return None
    if not any((p.get("spec") or {}).get("priorityClassName") == critical
               for p in kget("/api/v1/pods").get("items", [])):
        return None
    if operations and any(i.get("status") not in operations.TERMINAL for i in operations.list_operations()):
        return None
    ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}",
          {"spec": {"template": {"spec": {"priorityClassName": critical, "priority": None, "preemptionPolicy": None}}}},
          ctype="application/strategic-merge-patch+json")
    return f"Homestead runs as {critical} now, so no critical app can evict it; it restarts once"


def run(sleep=time.sleep):
    """Every minute on the leader: Homestead's own tier (protect)."""
    while True:
        if is_leader():
            try:
                said = protect()
                if said:
                    print(f"priority: {said}", flush=True)
            except Exception as error:
                print(f"priority: {str(error)[:160]}", flush=True)
        sleep(60)


def _picks(query):
    """?chosen=ns/app=tier,...: the tiers asked for, or None for the defaults."""
    chosen = query.get("chosen")
    return dict(item.split("=", 1) for item in chosen[0].split(",") if "=" in item) if chosen else None


ROUTES = {
    ("GET", "/api/workloads/priority/plan"): ("viewer", lambda request: plan(_picks(request.query), request.role)),
    ("POST", "/api/workloads/priority"): ("operator", lambda request: start(request.body, request.role)),
}
