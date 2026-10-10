"""Setting what apps reserve, many at once, from what they have used.

Edit offers one container's reservation from its week of use (homestead_usage).
On a cluster where most apps reserve nothing - 28 of 48 on the one in #347 -
that is an afternoon of dialogs, so this offers them together: a plan of each
container whose use suggests a different reservation, reviewed as a list,
then applied by a job one app at a time.

What is offered is what Homestead may change: a Deployment outside the system
namespaces, not Homestead itself (its manifest sets its own on every
upgrade), and not one Flux or Argo CD applies - their next sync would undo the
change, so those get the YAML to commit instead. An app with less than a day
of readings is left out with the reason.

Containers with no reservation at all are ticked; ones whose reservation only
differs from their use are offered unticked, since someone may have set them
on purpose. Each app restarts once, with all its containers' changes in one
rollout, and the next waits until it is running again. One that does not
come back within ten minutes stops the job, so a problem is looked at before
more restarts follow.
"""
import hashlib
import json
import time
import urllib.error

KIND = "set-requests"
MAX_APPS = 50
SYSTEM = ("kube-", "cattle-", "harvester-", "longhorn-", "fleet-")

kget = ksend = None
usage = None                     # homestead_usage.summary
managed_by = lambda dep: {}      # homestead_updates.managed_by
own = ("", "")                   # Homestead's namespace and Deployment
system_ns = frozenset()
is_leader = lambda: True


def bind(_kget, _ksend, _usage, _managed_by, _own, _system_ns, _is_leader=None):
    global kget, ksend, usage, managed_by, own, system_ns, is_leader
    kget, ksend, usage, managed_by, own, system_ns = _kget, _ksend, _usage, _managed_by, tuple(_own), frozenset(_system_ns)
    if _is_leader:
        is_leader = _is_leader


def _deployments():
    for dep in kget("/apis/apps/v1/deployments").get("items", []):
        meta = dep.get("metadata") or {}
        ns = meta.get("namespace", "")
        if ns in system_ns or ns.startswith(SYSTEM):
            continue
        yield dep


def _resources(container):
    res = container.get("resources") or {}
    return res.get("requests") or {}, res.get("limits") or {}


def _yaml(changes):
    """The resources to commit where Flux or Argo CD reads the app from."""
    lines = []
    for c in changes:
        lines += [f"- name: {c['container']}", "  resources:", "    requests:",
                  f"      cpu: {c['cpu']}", f"      memory: {c['memory']}"]
    return "\n".join(lines)


def _changes(dep):
    """Each container of a Deployment whose use suggests another reservation,
    and the reason when none can be offered."""
    meta = dep.get("metadata") or {}
    ns, name = meta.get("namespace", ""), meta.get("name", "")
    out, waiting = [], []
    for container in (((dep.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []:
        requests, limits = _resources(container)
        found = usage(ns, name, container.get("name", ""), requests, limits)
        suggest = found.get("suggest")
        if not suggest:
            waiting.append(found.get("why") or "no readings yet")
            continue
        if not suggest.get("differs"):
            continue
        out.append({"container": container.get("name", ""),
                    "cpu_now": str(requests.get("cpu", "")), "memory_now": str(requests.get("memory", "")),
                    "cpu": suggest["cpu"], "memory": suggest["memory"], "capped": suggest.get("capped") or [],
                    "missing": not requests.get("cpu") and not requests.get("memory"),
                    "cpu_p95_m": found.get("cpu_p95_m"), "mem_p95_mi": found.get("mem_p95_mi"),
                    "mem_max_mi": found.get("mem_max_mi"), "days": found.get("days")})
    return out, waiting


def plan(chosen=None):
    """Every app whose reservations its use would change, and every one left
    out with why. chosen names the apps ticked - by default those reserving
    nothing - and the review token covers exactly those."""
    apps, skipped = [], []
    for dep in _deployments():
        meta = dep.get("metadata") or {}
        ns, name = meta.get("namespace", ""), meta.get("name", "")
        app_id = f"{ns}/{name}"
        changes, waiting = _changes(dep)
        if (ns, name) == own:
            if changes:
                skipped.append({"id": app_id, "why": "Homestead's own reservations come from its manifest, which every upgrade applies again"})
            continue
        owner = managed_by(dep)
        if changes and owner:
            where = f" ({owner['source']})" if owner.get("source") else ""
            skipped.append({"id": app_id, "why": f"{owner['by']} applies it{where} and would undo the change at its next sync; commit this instead",
                            "yaml": _yaml(changes)})
            continue
        if changes:
            apps.append({"id": app_id, "ns": ns, "name": name, "changes": changes,
                         "missing": any(c["missing"] for c in changes),
                         "replicas": int((dep.get("spec") or {}).get("replicas") or 0)})
        elif waiting and len(waiting) == len((((dep.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []):
            skipped.append({"id": app_id, "why": waiting[0]})
    apps.sort(key=lambda a: (not a["missing"], a["id"]))
    chosen = {a["id"] for a in apps if a["missing"]} if chosen is None else set(chosen)
    for app in apps:
        app["chosen"] = app["id"] in chosen
    return {"apps": apps, "skipped": sorted(skipped, key=lambda s: s["id"]), "max": MAX_APPS,
            "review_token": review_token([a for a in apps if a["chosen"]])}


def review_token(apps):
    """What was reviewed: which containers get which reservation, and what
    each had then - so a reservation changed since by hand is not overwritten."""
    rows = sorted([a["id"], c["container"], c["cpu_now"], c["memory_now"], c["cpu"], c["memory"]]
                  for a in apps for c in a["changes"])
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()[:20]


FIELDS = ("container", "cpu_now", "memory_now", "cpu", "memory")


def reviewed(apps, token):
    """The changes a person reviewed, checked against the cluster now."""
    if not isinstance(apps, list) or not apps:
        raise ValueError("Choose at least one app")
    if len(apps) > MAX_APPS:
        raise ValueError(f"At most {MAX_APPS} apps at a time")
    for app in apps:
        if not isinstance(app, dict) or not isinstance(app.get("id"), str) or "/" not in app["id"] \
                or not isinstance(app.get("changes"), list) or not app["changes"] \
                or any(not isinstance(c, dict) or not all(isinstance(c.get(k), str) for k in FIELDS) for c in app["changes"]):
            raise ValueError("Review the reservations again")
    if review_token(apps) != token:
        raise ValueError("The reservations differ from the review; review again")
    out = []
    for app in apps:
        ns, name = app["id"].split("/", 1)
        changes = [{k: c[k] for k in FIELDS} for c in app["changes"]]
        check(ns, name, changes)
        out.append({"id": app["id"], "ns": ns, "name": name, "changes": changes})
    return out


def check(ns, name, changes):
    """One app as it is now: still one Homestead may change, and each
    container still reserving what it did when reviewed - a reservation set
    by hand since is not overwritten. A limit lowered since is left to
    Kubernetes, which refuses a request above it."""
    app_id = f"{ns}/{name}"
    try:
        dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise ValueError(f"{app_id} is gone")
        raise
    if (ns, name) == own or ns in system_ns or ns.startswith(SYSTEM) or managed_by(dep):
        raise ValueError(f"{app_id} is not one Homestead changes")
    containers = {c.get("name"): c for c in (((dep.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []}
    for change in changes:
        container = containers.get(change["container"])
        if not container:
            raise ValueError(f"{app_id} has no container {change['container']} now")
        requests, _ = _resources(container)
        if (str(requests.get("cpu", "")), str(requests.get("memory", ""))) != (change["cpu_now"], change["memory_now"]):
            raise ValueError(f"{app_id}'s reservations changed since the review")


# --------------------------------------------------------------- the job
def _settled(ns, name):
    dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    status, spec, meta = dep.get("status") or {}, dep.get("spec") or {}, dep.get("metadata") or {}
    want = int(spec.get("replicas") or 0)
    return (int(status.get("observedGeneration") or 0) >= int(meta.get("generation") or 0)
            and int(status.get("updatedReplicas") or 0) == want
            and int(status.get("readyReplicas") or 0) == want
            and int(status.get("replicas") or 0) == want)


def patch(app):
    """All of one app's containers in one change, so it restarts once."""
    containers = [{"name": c["container"], "resources": {"requests": {"cpu": c["cpu"], "memory": c["memory"]}}}
                  for c in app["changes"]]
    ksend("PATCH", f"/apis/apps/v1/namespaces/{app['ns']}/deployments/{app['name']}",
          {"spec": {"template": {"spec": {"containers": containers}}}},
          ctype="application/strategic-merge-patch+json")


def status(item):
    ref = item["ref"]
    apps, i, done = ref["apps"], ref.get("index", 0), ref.get("done", 0)
    if i >= len(apps):
        skipped = ref.get("skipped") or []
        return "succeeded", 100, (f"Set reservations on {done} app{'' if done == 1 else 's'}"
                                  + (f"; left {len(skipped)}: " + "; ".join(skipped[:4]) if skipped else ""))
    app = apps[i]
    progress = int(100 * i / max(1, len(apps)))
    if not is_leader():
        return "running", progress, item.get("message") or f"Setting {app['id']}"
    stage, now = ref.get("stage", "apply"), time.time()
    if stage == "apply":
        try:
            check(app["ns"], app["name"], app["changes"])
            patch(app)
        except (ValueError, urllib.error.HTTPError) as error:
            # Refused - a limit lowered below the new reservation, say - or
            # changed since the review: this app is left, the rest go on.
            if isinstance(error, urllib.error.HTTPError) and error.code not in (403, 409, 422):
                raise
            why = str(error) if isinstance(error, ValueError) else f"Kubernetes refused it ({error.code})"
            ref.setdefault("skipped", []).append(f"{app['id']}: {why[:160]}")
            ref.update(index=i + 1)
            return "running", progress, f"Skipped {app['id']}: {why[:160]}"
        ref.update(stage="rolling", started=now)
        return "running", progress, f"Setting {app['id']} ({i + 1} of {len(apps)}); it restarts"
    if stage == "rolling":
        if _settled(app["ns"], app["name"]):
            ref.update(index=i + 1, stage="apply", done=done + 1)
            return "running", int(100 * (i + 1) / len(apps)), f"{app['id']} is running with its new reservations"
        if now - ref.get("started", now) > 600:
            # Stop rather than restart more: one that did not come back is looked at first.
            return "failed", progress, (f"{app['id']} did not come back within 10 minutes; the rest were not changed. "
                                        f"Set {done} before it. Check its events in Containers")
        return "running", progress, f"Waiting for {app['id']} to restart ({i + 1} of {len(apps)})"
    return "failed", progress, "Unknown step; the rest were not changed"


def cancel_plan(item):
    return {"mode": "stop", "action": "Stop setting reservations",
            "undo": ["No more apps are changed"],
            "keeps": ["An app already restarting finishes with its new reservations",
                      "Apps already changed keep their new reservations"], "needs": "operator"}


def cancel_run(work, chosen):
    done = (work.get("ref") or {}).get("done", 0)
    return f"Stopped after setting {done} app{'' if done == 1 else 's'}"
