"""Priority tiers: which apps fit first when the cluster is short of room.

When a host fails, its apps start on the others; if those cannot hold them
all, the scheduler fits them in its own order, and the home automation can
wait behind a media scanner (#347). Kubernetes orders pods by their
PriorityClass, so Homestead keeps three of its own:

* critical - fits first, and may evict lower pods to fit (home automation,
  the password manager, a database, the tunnel);
* normal - every app without a class, as now: the class's value is 0, the
  same as having none, so "no class" and "normal" mean one thing;
* low - waits, and never evicts anything (preemptionPolicy Never): batch
  work and CI runners.

None is the cluster's globalDefault, which would apply to every new pod -
system pods included. An app's tier is read from its pod spec, so a class
set by hand or by Flux is shown as it is; a class Homestead does not keep is
shown by name and left alone.

Changing a tier changes the pod template, so the app restarts once. Like the
reservations (homestead_requests), the tiers are reviewed as a list and
applied by a job one app at a time; Flux- and Argo-applied apps get the line
to commit instead. Homestead's own Deployment is left alone: its manifest is
applied again on every upgrade and would put it back.
"""
import hashlib
import json
import re
import time
import urllib.error

KIND = "set-priority"
MAX_APPS = 50
SYSTEM = ("kube-", "cattle-", "harvester-", "longhorn-", "fleet-")
TIERS = {
    "critical": {"name": "homestead-critical", "value": 1000, "preemptionPolicy": "PreemptLowerPriority",
                 "description": "Homestead: fits first, and may evict lower-priority pods to fit."},
    "normal": {"name": "homestead-normal", "value": 0, "preemptionPolicy": "PreemptLowerPriority",
               "description": "Homestead: the same as no class at all."},
    "low": {"name": "homestead-low", "value": -1000, "preemptionPolicy": "Never",
            "description": "Homestead: waits for room, and never evicts anything."},
}
BY_CLASS = {spec["name"]: tier for tier, spec in TIERS.items()}
# What an app is, from its name or image, when it is likely critical or low.
# A suggestion only: it is ticked in the review, never applied unseen.
HINTS = [
    ("critical", re.compile(r"home-?assistant|zigbee2mqtt|z-?wave|mosquitto|vaultwarden|bitwarden|"
                            r"postgres|mariadb|mysql|cloudflared|tailscale|wireguard|pi-?hole|adguard")),
    ("low", re.compile(r"actions-runner|gitlab-runner|renovate|\brunner\b")),
]
WHY = {"critical": "looks like home automation, a password manager, a database, DNS or a tunnel",
       "low": "looks like batch or CI work"}

kget = ksend = None
managed_by = lambda dep: {}
own = ("", "")
system_ns = frozenset()
is_leader = lambda: True


def bind(_kget, _ksend, _managed_by, _own, _system_ns, _is_leader=None):
    global kget, ksend, managed_by, own, system_ns, is_leader
    kget, ksend, managed_by, own, system_ns = _kget, _ksend, _managed_by, tuple(_own), frozenset(_system_ns)
    if _is_leader:
        is_leader = _is_leader


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
    for tier, pattern in HINTS:
        if pattern.search(text):
            return tier, WHY[tier]
    return None, ""


def ensure():
    """The three classes, made where missing. One that exists with another
    value is kept as it is - someone made it - and said so."""
    notes = []
    for tier, spec in TIERS.items():
        try:
            found = kget(f"/apis/scheduling.k8s.io/v1/priorityclasses/{spec['name']}")
            if found.get("value") != spec["value"]:
                notes.append(f"{spec['name']} exists with value {found.get('value')}; it is kept")
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            ksend("POST", "/apis/scheduling.k8s.io/v1/priorityclasses", {
                "apiVersion": "scheduling.k8s.io/v1", "kind": "PriorityClass",
                "metadata": {"name": spec["name"], "labels": {"app.kubernetes.io/managed-by": "homestead"}},
                "value": spec["value"], "preemptionPolicy": spec["preemptionPolicy"],
                "globalDefault": False, "description": spec["description"]})
    return notes


def _deployments():
    for dep in kget("/apis/apps/v1/deployments").get("items", []):
        ns = (dep.get("metadata") or {}).get("namespace", "")
        if ns not in system_ns and not ns.startswith(SYSTEM):
            yield dep


def _pod_spec(dep):
    return ((dep.get("spec") or {}).get("template") or {}).get("spec") or {}


def plan(chosen=None):
    """Every app with its tier now, the tier suggested, and the tier it is
    set to in the review: by default the suggestion for an app still at
    normal, else what it has. chosen maps an app id to a tier."""
    apps, skipped = [], []
    for dep in _deployments():
        meta = dep.get("metadata") or {}
        ns, name = meta.get("namespace", ""), meta.get("name", "")
        app_id = f"{ns}/{name}"
        current, cls = tier_of(_pod_spec(dep))
        suggested, why = suggestion(dep)
        if (ns, name) == own:
            continue
        if current == "other":
            skipped.append({"id": app_id, "why": f"it uses the class {cls}, which Homestead does not keep; it is left alone"})
            continue
        owner = managed_by(dep)
        row = {"id": app_id, "ns": ns, "name": name, "current": current, "suggested": suggested or "", "why": why,
               "managed": owner.get("by", "")}
        if owner:
            where = f" ({owner['source']})" if owner.get("source") else ""
            row["note"] = f"{owner['by']} applies it{where}; set priorityClassName there"
        default = suggested if suggested and current == "normal" and not owner else current
        row["tier"] = (chosen or {}).get(app_id, default) if not owner else current
        if row["tier"] not in TIERS:
            raise ValueError(f"{app_id}: the tier must be critical, normal or low")
        apps.append(row)
    apps.sort(key=lambda a: ({"critical": 0, "normal": 1, "low": 2}[a["tier"]], a["id"]))
    changes = [a for a in apps if a["tier"] != a["current"]]
    return {"apps": apps, "skipped": sorted(skipped, key=lambda s: s["id"]), "max": MAX_APPS,
            "changes": len(changes), "review_token": review_token(changes)}


def review_token(changes):
    rows = sorted([c["id"], c["current"], c["tier"]] for c in changes)
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()[:20]


def reviewed(changes, token):
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
        ns, name = c["id"].split("/", 1)
        check(ns, name, c["current"])
        out.append({"id": c["id"], "ns": ns, "name": name, "current": c["current"], "tier": c["tier"]})
    return out


def check(ns, name, current):
    app_id = f"{ns}/{name}"
    try:
        dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise ValueError(f"{app_id} is gone")
        raise
    if (ns, name) == own or ns in system_ns or ns.startswith(SYSTEM) or managed_by(dep):
        raise ValueError(f"{app_id} is not one Homestead changes")
    if tier_of(_pod_spec(dep))[0] != current:
        raise ValueError(f"{app_id}'s tier changed since the review")


def patch(app):
    """Normal is no class at all: the class is removed rather than set."""
    cls = None if app["tier"] == "normal" else TIERS[app["tier"]]["name"]
    ksend("PATCH", f"/apis/apps/v1/namespaces/{app['ns']}/deployments/{app['name']}",
          {"spec": {"template": {"spec": {"priorityClassName": cls}}}},
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
        ref["notes"] = ensure()
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
            check(app["ns"], app["name"], app["current"])
            patch(app)
        except (ValueError, urllib.error.HTTPError) as error:
            if isinstance(error, urllib.error.HTTPError) and error.code not in (403, 409, 422):
                raise
            why = str(error) if isinstance(error, ValueError) else f"Kubernetes refused it ({error.code})"
            ref.setdefault("skipped", []).append(f"{app['id']}: {why[:160]}")
            ref.update(index=i + 1)
            return "running", progress, f"Skipped {app['id']}: {why[:160]}"
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
