"""Restore into this app (#743): a backup is restored into a new claim, and the
app is moved onto it - the swap the wiki used to leave to the person.

A restore still never overwrites a volume. The job, step by step, each
checkpointed before it writes so a restart carries on and repeats nothing:

  1. restoring: the backup goes into a new claim, as /api/lh/restore does; the
     app keeps running meanwhile;
  2. stopping: the Deployment's replicas are recorded, then it is scaled to 0
     and Homestead waits until no pod uses its claim;
  3. swapping: only the template volume that mounts the claim is pointed at
     the new one, guarded by the Deployment's generation so an edit made
     meanwhile is never overwritten; the old claim is marked as the app's
     previous volume;
  4. starting: the replicas are put back;
  5. watching: the checks of an automatic update - ready in time, then the
     uptime check answers (or, with no check, it stays ready).

If the app does not come up on the restored data, it is swapped back on its
own and the job fails, saying why: the restored claim is then the previous
volume, kept for a person to look at. A failure before the swap starts the app
again unchanged.

A previous volume carries its marks on the claim itself, so whichever replica
leads carries the expiry on: it is deleted keep_days after it was replaced
(Settings, default 7; 0 keeps it until a person deletes it), never less than a
day after its reminder was raised, and never while anything mounts it.
"""
import contextlib
import datetime
import hashlib
import json
import re
import secrets
import time
import urllib.error
import urllib.parse

import homestead_alerts as ALERTS
import homestead_autoupdate as AUTOUPDATE
import homestead_cancel as CANCEL
import homestead_kube as KUBE
import homestead_leader as LEADER
import homestead_longhorn as LH
import homestead_names as NAMES
import homestead_operations as OPS
import homestead_reclass as RECLASS
import homestead_revert as REVERT
import homestead_routes as MODULE_ROUTES
import homestead_storage_guard as STORAGE_GUARD
import homestead_updates as UPDATES
import homestead_volumes as VOLUMES

KIND = "restore-swap"
STOP_WAIT = 600                   # the app's pods have this long to let go of the claim
DAY = 86400
DEFAULT_KEEP_DAYS = 7
PREVIOUS = NAMES.key("previous-volume")         # label: "true" on a kept previous volume
KEEP = NAMES.key("keep-previous")               # annotation: "true" when a person chose Keep
REMINDED = NAMES.key("reminded-at")
RESTORED_INTO = NAMES.key("restored-into")
MARKS = tuple(NAMES.key(k) for k in ("previous-of", "replaced-by", "replaced-at", "replaced-job", "replaced-uid",
                                      "replaced-reason", "reminded-at", "keep-previous"))
TERMINAL = ("succeeded", "failed", "cancelled")
# Kinds whose running job holds a claim this job must not touch.
HOLDING = ("reclass", "snapshot-revert", "snapshot-delete", "volume-delete", "volume-restore", KIND)
NAME = re.compile(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?")


# What it reads and writes through, looked up when called so a test can put
# its own in place (HOOKS lists them).
def kget(path, **options):
    return KUBE.kget(path, **options)


def ksend(method, path, body=None, ctype="application/json", **options):
    return KUBE.ksend(method, path, body, ctype, **options)


def fence(handle):
    """Storage moves and this job never meet on one volume (revert does the same)."""
    return STORAGE_GUARD.volume(OPS, kget, handle) if handle else contextlib.nullcontext()


backup_object = lambda name: LH._backup(name)
restore_plan = lambda backup, ns, name: LH.restore_plan(backup, ns, name)
restore_backup = lambda cfg: LH.restore_backup(cfg)
restore_resolver = lambda item: OPS.RESOLVERS["volume-restore"](item)
restore_cancel = lambda item: CANCEL.restore_cancel(item, {})
uptime_report = lambda: AUTOUPDATE.uptime_report()
is_self = lambda ns, name: REVERT.is_self(ns, name)
system_namespaces = lambda: VOLUMES.SYSTEM_NAMESPACES
is_leader = lambda: LEADER.is_leader()
note = lambda fact: ALERTS.note(fact)
deletion_plan = lambda ns, name: VOLUMES.deletion_plan(ns, name)
delete_volume = lambda cfg: VOLUMES.delete_reviewed(cfg)


def settings():
    import homestead_settings_helpers as SETTINGS_HELPERS
    return SETTINGS_HELPERS.get_app_settings()


def save_settings(change):
    import homestead_settings_helpers as SETTINGS_HELPERS
    return SETTINGS_HELPERS.save_app_settings(change)


HOOKS = ("kget", "ksend", "fence", "OPS", "backup_object", "restore_plan", "restore_backup", "restore_resolver",
         "restore_cancel", "uptime_report", "is_self", "system_namespaces", "is_leader", "note", "deletion_plan",
         "delete_volume", "settings", "save_settings")


# ---- small helpers -----------------------------------------------------------

def _valid(value, what):
    value = str(value or "").strip()
    if not NAME.fullmatch(value):
        raise ValueError(f"{what} must be a valid Kubernetes name")
    return value


def _pvc_path(ns, name):
    return f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}"


def _dep_path(ns, name):
    return f"/apis/apps/v1/namespaces/{ns}/deployments/{name}"


def _digest(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:20]


def keep_days(values=None):
    try:
        days = int(((values if values is not None else settings()) or {}).get("restore_swap", {}).get("keep_days", DEFAULT_KEEP_DAYS))
    except (TypeError, ValueError):
        days = DEFAULT_KEEP_DAYS
    return days


def _until(at, days):
    if not days:
        return "until you delete it"
    return "until " + datetime.datetime.fromtimestamp(at + days * DAY, datetime.timezone.utc).strftime("%-d %b")


def _volumes(dep):
    return ((dep.get("spec") or {}).get("template") or {}).get("spec", {}).get("volumes") or []


def _indexes(dep, claim):
    """Which template volumes mount the claim."""
    return [i for i, v in enumerate(_volumes(dep)) if (v.get("persistentVolumeClaim") or {}).get("claimName") == claim]


def _replicas(dep):
    value = (dep.get("spec") or {}).get("replicas")
    return 1 if value is None else int(value)


def _handle(pvc):
    """The Longhorn volume behind a claim, for the storage fence."""
    pv_name = (pvc.get("spec") or {}).get("volumeName")
    if not pv_name:
        return ""
    pv = KUBE.get_optional(f"/api/v1/persistentvolumes/{pv_name}", read=kget) or {}
    csi = (pv.get("spec") or {}).get("csi") or {}
    return csi.get("volumeHandle", "") if csi.get("driver") == "driver.longhorn.io" else ""


SPECS = (("Deployment", "apps/v1", "deployments", lambda o: o["spec"]["template"]["spec"]),
         ("StatefulSet", "apps/v1", "statefulsets", lambda o: o["spec"]["template"]["spec"]),
         ("DaemonSet", "apps/v1", "daemonsets", lambda o: o["spec"]["template"]["spec"]),
         ("CronJob", "batch/v1", "cronjobs", lambda o: o["spec"]["jobTemplate"]["spec"]["template"]["spec"]))


def _users(ns, claim):
    """What mounts the claim: workloads, and pods that belong to none of them."""
    found = []
    for kind, group, plural, spec_of in SPECS:
        for obj in kget(f"/apis/{group}/namespaces/{ns}/{plural}").get("items", []):
            try:
                if claim in RECLASS._claims_in(spec_of(obj)):
                    found.append({"kind": kind, "name": obj["metadata"]["name"], "obj": obj})
            except (KeyError, TypeError):
                raise ValueError("The workload list is incomplete; what uses the volume cannot be checked") from None
    names = [u["name"] for u in found]
    loose = [p["metadata"]["name"] for p in _pods_using(ns, claim)
             if not any(p["metadata"]["name"].startswith(n + "-") for n in names)]
    return found, loose


def _pods_using(ns, claim):
    return [p for p in kget(f"/api/v1/namespaces/{ns}/pods").get("items", [])
            if claim in RECLASS._claims_in(p.get("spec")) and (p.get("status") or {}).get("phase") not in ("Succeeded", "Failed")]


def _names_in(ref):
    return {n for n in (ref.get("claim"), ref.get("name"), ref.get("temp"), ref.get("old_claim"), ref.get("new_claim")) if n}


def _busy(items, ns, app, claims):
    """A job still running on the app or one of the claims."""
    for item in items or []:
        if item.get("status") in TERMINAL:
            continue
        ref, resource = item.get("ref") or {}, item.get("resource") or {}
        where = ref.get("namespace") or resource.get("namespace")
        if where != ns:
            continue
        if item.get("kind") == KIND and ref.get("app") == app:
            return item
        if item.get("kind") in HOLDING and (_names_in(ref) | {resource.get("name")}) & set(claims):
            return item
        if resource.get("kind") == "Deployment" and resource.get("name") == app:
            return item
    return None


# ---- the review --------------------------------------------------------------

def _backup_claim(status):
    try:
        k8s = json.loads((status.get("labels") or {}).get("KubernetesStatus") or "{}") or {}
    except (TypeError, ValueError):
        k8s = {}
    return k8s.get("namespace", ""), k8s.get("pvcName", "")


def _free_name(backup, ns, claim, wanted):
    """The new claim's name and the restore plan for it: the one asked for, or
    <claim>-restore, -restore-2 ... when that is taken."""
    if wanted:
        return wanted, restore_plan(backup, ns, wanted)
    base = (claim[:240] + "-restore")
    for n in range(1, 10):
        name = base if n == 1 else f"{base}-{n}"
        found = restore_plan(backup, ns, name)
        if not found.get("conflict"):
            return name, found
    return base, restore_plan(backup, ns, base)


def plan(backup, ns="", app="", claim="", new_name="", storage_class="", size_gb=None, now=None):
    """What restoring into the app would do and what stops it, before anything changes."""
    now = now or time.time()
    item = backup_object(str(backup or ""))
    status = item.get("status") or {}
    blockers, notices = [], []
    try:
        LH._restore_source(item)
    except ValueError as error:
        blockers.append(str(error))
    source_ns, source_claim = _backup_claim(status)
    ns, claim = ns or source_ns, claim or source_claim
    if not ns or not claim:
        raise ValueError("This backup does not say which volume it came from; choose the app and its volume")
    ns, claim = _valid(ns, "namespace"), _valid(claim, "volume name")
    if new_name:
        new_name = _valid(new_name, "the new volume's name")
    if ns in system_namespaces():
        blockers.append("system namespaces are changed with Kubernetes administration tools, not here")
    pvc = KUBE.get_optional(_pvc_path(ns, claim), read=kget)
    if pvc is None:
        raise ValueError(f"{ns}/{claim} no longer exists; restore the backup into a new volume instead")
    users, loose = _users(ns, claim)
    deployments = [u for u in users if u["kind"] == "Deployment"]
    if not app:
        app = deployments[0]["name"] if len(deployments) == 1 else (users[0]["name"] if len(users) == 1 else "")
    app = _valid(app, "app") if app else ""
    mine = next((u for u in users if u["name"] == app), None)
    dep = mine["obj"] if mine and mine["kind"] == "Deployment" else {}
    if not app or not mine:
        blockers.append(f"no app mounts {claim}; restore the backup into a new volume instead")
    elif mine["kind"] != "Deployment":
        blockers.append(f"{app} is a {mine['kind']}: restoring into one is not supported yet; "
                        "restore the backup into a new volume instead")
    others = [u for u in users if u is not mine]
    if others:
        blockers.append(f"{claim} is also used by {', '.join(u['kind'] + ' ' + u['name'] for u in others[:3])}; "
                        "only a volume one app uses can be swapped")
    if loose:
        blockers.append(f"{', '.join(loose[:3])} use{'s' if len(loose) == 1 else ''} {claim} and belong"
                        f"{'s' if len(loose) == 1 else ''} to nothing Homestead can stop and start again; stop it first")
    if app and is_self(ns, app) or claim == "homestead-data":
        blockers.append("this is Homestead's own data: swapping it would stop Homestead part-way")
    name, found = _free_name(item["metadata"]["name"], ns, claim, new_name)
    if found.get("conflict"):
        blockers.append(found["conflict"]["message"])
    busy = _busy(OPS.snapshot(), ns, app, (claim, name))
    if busy:
        blockers.append(f"{app or claim} already has a job running ({busy.get('title') or busy.get('kind')}); "
                        "let it finish first")
    managed = UPDATES.managed_by(dep) if dep else {}
    if managed:
        notices.append(f"{app} is applied by {managed['by']}"
                       + (f" ({managed['source']})" if managed.get("source") else "")
                       + f": its next sync puts {claim} back. Change the claim where {managed['by']} reads it too.")
    indexes = _indexes(dep, claim) if dep else []
    volume = _volumes(dep)[indexes[0]]["name"] if indexes else ""
    containers = [{"name": c.get("name", ""), "path": m.get("mountPath", "")}
                  for c in ((dep.get("spec") or {}).get("template") or {}).get("spec", {}).get("containers") or []
                  for m in c.get("volumeMounts") or [] if m.get("name") == volume] if dep else []
    classes = found.get("storage_classes") or []
    current_class = (pvc.get("spec") or {}).get("storageClassName", "")
    chosen_class = storage_class or (current_class if current_class in classes else found.get("storage_class", ""))
    size = int(size_gb or found.get("minimum_size_gb") or 1)
    days = keep_days()
    answers = uptime_report().get(f"{ns}/{app}") or {}
    meta = dep.get("metadata") or {}
    out = {
        "app": {"namespace": ns, "name": app, "kind": mine["kind"] if mine else "", "replicas": _replicas(dep) if dep else 0},
        "claim": claim, "mount": {"volume": volume, "containers": containers},
        "backup": item["metadata"]["name"], "created": status.get("backupCreatedAt", ""),
        "new_claim": name, "storage_class": chosen_class, "storage_classes": classes, "size_gb": size,
        "minimum_size_gb": found.get("minimum_size_gb"),
        "steps": ["restore", "stop", "swap", "start", "watch"],
        "check": "uptime" if answers and answers.get("state") != "off" else "ready",
        "keep_days": days, "expires_preview": "" if not days else datetime.datetime.fromtimestamp(
            now + days * DAY, datetime.timezone.utc).strftime("%Y-%m-%d"),
        "managed": managed, "blockers": blockers, "notices": notices,
    }
    out["digest"] = _digest(ns, app, claim, (pvc.get("metadata") or {}).get("uid"), meta.get("generation"),
                            out["backup"], name, chosen_class, size)
    out["ready"] = not blockers
    out["_internal"] = {"pvc": pvc, "dep": dep, "indexes": indexes}
    return out


def public(p):
    return {k: v for k, v in p.items() if not k.startswith("_")}


def start(body):
    """Restore and swap, as reviewed: a 409 with a fresh plan when the review is stale."""
    b = body or {}
    with OPS.transaction():
        p = plan(b.get("backup"), b.get("namespace") or "", b.get("app") or "", b.get("claim") or "",
                 b.get("new_name") or "", b.get("storage_class") or "", b.get("size_gb"))
        if str(b.get("digest") or "") != p["digest"]:
            return MODULE_ROUTES.Reply(409, {"error": "The app, its volume or the backup changed since this review; "
                                                      "check the plan again", "plan": public(p)})
        if p["blockers"]:
            raise ValueError(p["blockers"][0])
        if p["managed"] and b.get("accept_gitops") is not True:
            raise ValueError(f"{p['app']['name']} is applied by {p['managed']['by']}, whose next sync puts the old "
                             "volume back; confirm you have read that to go on")
        ns, app, old, new = p["app"]["namespace"], p["app"]["name"], p["claim"], p["new_claim"]
        pvc, dep = p["_internal"]["pvc"], p["_internal"]["dep"]
        restore_id = secrets.token_hex(12)
        cfg = {"backup": p["backup"], "namespace": ns, "name": new, "storage_class": p["storage_class"],
               "size_gb": p["size_gb"], "access_mode": ((pvc.get("spec") or {}).get("accessModes") or ["ReadWriteOnce"])[0],
               "restore_id": restore_id,
               "annotations": {"homestead.io/restore-id": restore_id, RESTORED_INTO: f"{ns}/{app}"}}
        result = restore_backup(cfg)
        cfg["source_size_bytes"] = result.get("source_size_bytes")
        ref = {"namespace": ns, "app": app, "kind": "Deployment", "volume": p["mount"]["volume"],
               "indexes": p["_internal"]["indexes"], "old_claim": old, "new_claim": new,
               "old_uid": (pvc.get("metadata") or {}).get("uid", ""), "old_volume": _handle(pvc),
               "backup": p["backup"], "generation": (dep.get("metadata") or {}).get("generation"),
               "direction": "restore", "phase": "restoring", "since": time.time(),
               "restore": {"namespace": ns, "name": new, "backup": p["backup"], "restore_config": cfg,
                           "restore_started": bool(result.get("created"))}}
        operation = OPS.start(KIND, f"Restore {app} from a backup", {"kind": "Deployment", "name": app, "namespace": ns},
                              "/data-protection", ref, result.get("message") or "Restoring")
    return {"ok": True, "operation": operation, "name": new,
            "detail": f"Restoring into {new}; {app} keeps running until the data is ready"}


# ---- the job ----------------------------------------------------------------

def _scale(ref, replicas):
    return ksend("PATCH", _dep_path(ref["namespace"], ref["app"]), {"spec": {"replicas": replicas}},
                 ctype="application/merge-patch+json")


def _point(ref, generation, frm, to):
    """Only the template volumes that mount frm, and only if nothing else
    changed the Deployment since: Kubernetes refuses the whole patch otherwise."""
    ops = [{"op": "test", "path": "/metadata/generation", "value": generation}] if generation is not None else []
    for i in ref["indexes"]:
        where = f"/spec/template/spec/volumes/{i}/persistentVolumeClaim/claimName"
        ops += [{"op": "test", "path": where, "value": frm}]
    for i in ref["indexes"]:
        ops += [{"op": "replace", "path": f"/spec/template/spec/volumes/{i}/persistentVolumeClaim/claimName", "value": to}]
    return ksend("PATCH", _dep_path(ref["namespace"], ref["app"]), ops, ctype="application/json-patch+json")


def _points_at(dep, ref):
    claims = {(_volumes(dep)[i].get("persistentVolumeClaim") or {}).get("claimName")
              for i in ref["indexes"] if i < len(_volumes(dep))}
    return claims.pop() if len(claims) == 1 else ""


def _mark(item, claim, replaced_by, reason, now):
    """Keep a claim as the app's previous volume, from now."""
    ref = item["ref"]
    pvc = KUBE.get_optional(_pvc_path(ref["namespace"], claim), read=kget)
    if not pvc:
        return
    notes = (pvc.get("metadata") or {}).get("annotations") or {}
    if notes.get(NAMES.key("replaced-job")) == item.get("id") and notes.get(NAMES.key("replaced-reason")) == reason:
        return          # marked by this job already: a restart keeps its date
    ksend("PATCH", _pvc_path(ref["namespace"], claim), {"metadata": {
        "labels": {PREVIOUS: "true"},
        "annotations": {NAMES.key("previous-of"): f"{ref['namespace']}/{ref['app']}",
                        NAMES.key("replaced-by"): replaced_by, NAMES.key("replaced-at"): str(int(now)),
                        NAMES.key("replaced-job"): item.get("id", ""),
                        NAMES.key("replaced-uid"): (pvc.get("metadata") or {}).get("uid", ""),
                        NAMES.key("replaced-reason"): reason, REMINDED: None, KEEP: None}}},
          ctype="application/merge-patch+json")


def _unmark(ns, claim):
    if KUBE.get_optional(_pvc_path(ns, claim), read=kget) is None:
        return
    ksend("PATCH", _pvc_path(ns, claim), {"metadata": {"labels": {PREVIOUS: None},
                                                       "annotations": {k: None for k in MARKS}}},
          ctype="application/merge-patch+json")


def _days():
    try:
        return keep_days()
    except Exception:
        return DEFAULT_KEEP_DAYS


def _put_back(item, why, now):
    """Before the swap: the app runs again as it was; a restored claim that
    was made is kept as a previous volume, so it expires."""
    ref = item["ref"]
    with fence(ref.get("old_volume")):
        if ref.get("stopped"):
            _scale(ref, ref.get("replicas_before", 1))
        made = ""
        if ref["direction"] == "restore":
            pvc = KUBE.get_optional(_pvc_path(ref["namespace"], ref["new_claim"]), read=kget)
            restore_id = ref.get("restore", {}).get("restore_config", {}).get("restore_id")
            if pvc and ((pvc.get("metadata") or {}).get("annotations") or {}).get("homestead.io/restore-id") == restore_id:
                _mark(item, ref["new_claim"], ref["old_claim"], "not-used", now)
                made = ref["new_claim"]
    ref.update(phase="put-back", since=now)
    tail = f" The restored volume {made} is kept {_until(now, _days())}." if made else ""
    return "failed", 100, f"{why}. Nothing was changed; {ref['app']} runs on {ref['old_claim']} as before.{tail}"


def _swap_back_writes(item, now):
    """Point the app at the claim it left, and keep the other as the previous volume."""
    ref = item["ref"]
    with fence(ref.get("old_volume")):
        _scale(ref, 0)
        dep = kget(_dep_path(ref["namespace"], ref["app"]))
        if _points_at(dep, ref) != ref["old_claim"]:
            _point(ref, None, ref["new_claim"], ref["old_claim"])
        _unmark(ref["namespace"], ref["old_claim"])
        _mark(item, ref["new_claim"], ref["old_claim"], "not-started", now)
        _scale(ref, ref.get("replicas_before", 1))


def _pointed_at(ref):
    try:
        return _points_at(kget(_dep_path(ref["namespace"], ref["app"])), ref) or "a volume Homestead could not read"
    except Exception:
        return "a volume Homestead could not read"


def _swap_back(item, to, why, now):
    ref = item["ref"]
    if ref.get("phase") != "swapping-back":
        to("swapping-back", why=why)
    try:
        _swap_back_writes(item, now)
    except Exception as error:
        return ("failed", 100, f"On the restored volume {ref['app']} {why}, and swapping back did not work either: "
                f"{str(error)[:160]}. {ref['app']} now points at {_pointed_at(ref)}; "
                f"its previous volume is {ref['old_claim']}. Point it back with Edit.")
    to("back-starting", back_at=now)
    return "running", 95, f"Swapped back to {ref['old_claim']}; waiting for {ref['app']} to be ready"


def resolve(item, checkpoint, now=None):
    """Move one restore-into-app job on a step: (status, progress, message)."""
    now = now or time.time()
    ref = item["ref"]
    ns, app, old, new = ref["namespace"], ref["app"], ref["old_claim"], ref["new_claim"]
    phase = ref.get("phase", "restoring")

    def to(next_phase, **changes):
        ref.update(changes, phase=next_phase, since=now)
        checkpoint(item)

    if phase == "restoring":
        sub = {"id": item.get("id"), "status": item.get("status"), "ref": ref["restore"]}
        status, progress, message = restore_resolver(sub)
        if status == "running":
            return "running", min(40, int(progress * 0.4)), f"Restoring into {new}: {message}"
        if status != "succeeded":
            return _put_back(item, f"The restore did not finish ({message})", now)
        pvc = kget(_pvc_path(ns, new))
        to("stopping", new_uid=(pvc.get("metadata") or {}).get("uid", ""))
        phase = "stopping"
    if phase == "stopping":
        if not ref.get("stopped"):
            if "replicas_before" not in ref:
                to("stopping", replicas_before=_replicas(kget(_dep_path(ns, app))))
            with fence(ref.get("old_volume")):
                scaled = _scale(ref, 0)
            ref.update(stopped=True, generation=(scaled.get("metadata") or {}).get("generation"))
            checkpoint(item)
        using = _pods_using(ns, old)
        if using:
            if now - float(ref.get("since") or now) > STOP_WAIT:
                return _put_back(item, f"{app} did not let go of {old} in time - "
                                       f"{using[0]['metadata']['name']} still uses it", now)
            return "running", 45, f"Stopping {app}"
        to("swapping")
        phase = "swapping"
    if phase == "swapping":
        dep = kget(_dep_path(ns, app))
        if _points_at(dep, ref) != new:
            try:
                with fence(ref.get("old_volume")):
                    _point(ref, ref.get("generation"), old, new)
            except urllib.error.HTTPError as error:
                if error.code not in (409, 422):
                    raise
                return _put_back(item, f"{app} was changed while it was stopped, so it was not swapped", now)
        _mark(item, old, new, "restored" if ref["direction"] == "restore" else "swapped-back", now)
        if ref["direction"] == "restore":
            ksend("PATCH", _pvc_path(ns, new), {"metadata": {"annotations": {RESTORED_INTO: f"{ns}/{app}"}}},
                  ctype="application/merge-patch+json")
        else:
            _unmark(ns, new)
        to("starting", swapped=True)
        phase = "starting"
    if phase == "starting":
        if ref.get("replicas_before", 1) == 0:
            return "succeeded", 100, f"{app} now uses {new}; it stays stopped, as it was. {_kept(ref, now)}"
        with fence(ref.get("old_volume")):
            _scale(ref, ref.get("replicas_before", 1))
        to("watching", started_at=now)
        return "running", 70, f"{app} is starting on {new}"
    if phase == "watching":
        dep = kget(_dep_path(ns, app))
        if not ref.get("rolled_at"):
            if AUTOUPDATE._rolled_out(dep):
                to("watching", rolled_at=now)
                return "running", 80, f"{app} is ready; waiting for it to answer"
            if now - float(ref.get("started_at") or now) > AUTOUPDATE.ROLLOUT_WAIT:
                return _swap_back(item, to, f"was not ready after {AUTOUPDATE.ROLLOUT_WAIT // 60} minutes", now)
            return "running", 75, f"Waiting for {app} to be ready"
        if not AUTOUPDATE._rolled_out(dep):
            return _swap_back(item, to, "stopped being ready", now)
        state = uptime_report().get(f"{ns}/{app}") or {}
        last = state.get("last") or {}
        fresh = (last.get("at") or 0) > ref["rolled_at"]
        if state.get("state") == "off" or not state:
            if now - ref["rolled_at"] >= AUTOUPDATE.STEADY:
                return "succeeded", 100, f"{app} runs on {new}. {_kept(ref, now)}"
            return "running", 85, f"{app} is ready; making sure it stays up"
        if fresh and state.get("state") in ("up", "slow"):
            return "succeeded", 100, f"{app} runs on {new}. {_kept(ref, now)}"
        if fresh and state.get("state") == "down":
            return _swap_back(item, to, f"stopped answering ({last.get('error') or 'no answer'})", now)
        if now - ref["rolled_at"] > AUTOUPDATE.ANSWER_WAIT:
            return _swap_back(item, to, f"did not answer within {AUTOUPDATE.ANSWER_WAIT // 60} minutes", now)
        return "running", 88, f"Waiting for {app} to answer"
    if phase == "swapping-back":
        return _swap_back(item, to, ref.get("why") or "did not come up", now)
    if phase == "back-starting":
        dep = kget(_dep_path(ns, app))
        said = (f"Swapped back: on the {'restored' if ref['direction'] == 'restore' else 'other'} volume {app} "
                f"{ref.get('why') or 'did not come up'}. It runs on its previous volume {old} again. "
                f"The {'restored' if ref['direction'] == 'restore' else 'other'} volume {new} is kept "
                f"{_until(float(ref.get('back_at') or now), _days())} for you to look at.")
        if ref.get("replicas_before", 1) == 0 or AUTOUPDATE._rolled_out(dep):
            return "failed", 100, said
        if now - float(ref.get("back_at") or now) > AUTOUPDATE.ROLLOUT_WAIT:
            return "failed", 100, said + f" It is not ready on {old} yet either; look at its pods."
        return "running", 96, f"Swapped back to {old}; waiting for {app} to be ready"
    return "failed", item.get("progress", 0), f"Unknown step {phase}"


def _kept(ref, now):
    return f"Its previous volume {ref['old_claim']} is kept {_until(now, _days())}."


def cancel_plan(item):
    ref = item["ref"]
    if ref.get("phase") in ("swapping-back", "back-starting"):
        return {"mode": "stop", "can": False, "why_not": "It is swapping back already; let that finish"}
    if ref.get("swapped") or ref.get("phase") in ("starting", "watching"):
        return {"mode": "rollback", "needs": "admin", "severity": "low",
                "undo": [f"{ref['app']} is stopped, pointed back at {ref['old_claim']} and started again"],
                "keeps": [f"{ref['new_claim']} is kept as a previous volume, {_until(time.time(), _days())}"]}
    undo = ([f"The restore stops and {ref['new_claim']}, the volume it was making, is deleted"]
            if ref["direction"] == "restore" else [])
    return {"mode": "rollback", "needs": "admin", "severity": "low",
            "undo": undo + [f"{ref['app']} is started again, unchanged"],
            "keeps": ["The backup", f"{ref['old_claim']}, which has not been changed"]}


def cancel_run(item, _options):
    ref = item["ref"]
    now = time.time()
    swapped = ref.get("swapped")
    if not swapped and ref.get("phase") == "swapping":
        swapped = _points_at(kget(_dep_path(ref["namespace"], ref["app"])), ref) == ref["new_claim"]
    if swapped:
        _swap_back_writes(item, now)
        ref.update(phase="cancelled", since=now)
        return (f"Swapped back: {ref['app']} runs on {ref['old_claim']} again. "
                f"{ref['new_claim']} is kept {_until(now, _days())}.")
    if ref["direction"] == "restore":
        restore_cancel({"ref": {"namespace": ref["namespace"], "name": ref["new_claim"], "backup": ref["backup"]}})
    if ref.get("stopped"):
        with fence(ref.get("old_volume")):
            _scale(ref, ref.get("replicas_before", 1))
    ref.update(phase="cancelled", since=now)
    return f"Cancelled. Nothing was changed; {ref['app']} runs on {ref['old_claim']} as before."


# ---- previous volumes --------------------------------------------------------

def _labelled():
    """Every claim marked as a previous volume: one list, cluster-wide."""
    selector = urllib.parse.quote(f"{PREVIOUS}=true", safe="")
    return kget(f"/api/v1/persistentvolumeclaims?labelSelector={selector}").get("items", [])


def _number(notes, key):
    try:
        return float(notes.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def _due(notes, days):
    """When it would be deleted under today's setting: None when kept."""
    if notes.get(KEEP) == "true" or not days:
        return None
    expires = _number(notes, NAMES.key("replaced-at")) + days * DAY
    reminded = _number(notes, REMINDED)
    return max(expires, reminded + DAY) if reminded else expires


def _of(notes):
    ns_app = str(notes.get(NAMES.key("previous-of")) or "")
    return ns_app.split("/", 1)[1] if "/" in ns_app else ns_app


def _back_digest(ns, app, current, name, uid, dep):
    return _digest(ns, app, current, name, uid, ((dep or {}).get("metadata") or {}).get("generation"))


def _row(pvc, days, pods=None, dep=None):
    meta, spec = pvc.get("metadata") or {}, pvc.get("spec") or {}
    notes = meta.get("annotations") or {}
    ns, name, app = meta.get("namespace", ""), meta.get("name", ""), _of(notes)
    due, reminded = _due(notes, days), _number(notes, REMINDED)
    current = notes.get(NAMES.key("replaced-by"), "")
    return {"namespace": ns, "name": name, "app": app, "replaced_by": current,
            "replaced_at": int(_number(notes, NAMES.key("replaced-at"))) or None,
            "expires_at": int(due) if due else None, "reminded_at": int(reminded) if reminded else None,
            "kept": notes.get(KEEP) == "true", "reason": notes.get(NAMES.key("replaced-reason"), "restored"),
            "size": ((spec.get("resources") or {}).get("requests") or {}).get("storage", ""),
            "storage_class": spec.get("storageClassName", ""), "volume": spec.get("volumeName", ""),
            "uid": meta.get("uid", ""),
            "in_use": any(name in RECLASS._claims_in(p.get("spec")) for p in pods or []),
            "can_swap_back": bool(dep and _indexes(dep, current)),
            "digest": _back_digest(ns, app, current, name, meta.get("uid", ""), dep) if dep else ""}


def previous_volumes(now=None):
    """The volumes apps were moved off, and when each goes."""
    days = keep_days()
    pods, deps, rows = {}, {}, []
    for pvc in _labelled():
        meta = pvc.get("metadata") or {}
        ns, app = meta.get("namespace", ""), _of(meta.get("annotations") or {})
        if ns not in pods:
            pods[ns] = [p for p in kget(f"/api/v1/namespaces/{ns}/pods").get("items", [])
                        if (p.get("status") or {}).get("phase") not in ("Succeeded", "Failed")]
        if (ns, app) not in deps:
            deps[(ns, app)] = KUBE.get_optional(_dep_path(ns, app), read=kget) if app and NAME.fullmatch(app) else None
        rows.append(_row(pvc, days, pods[ns], deps[(ns, app)]))
    return {"keep_days": days, "volumes": sorted(rows, key=lambda r: (r["namespace"], r["name"]))}


def _previous_claim(body):
    b = body or {}
    ns, name = _valid(b.get("namespace"), "namespace"), _valid(b.get("name"), "volume name")
    pvc = KUBE.get_optional(_pvc_path(ns, name), read=kget)
    if not pvc or ((pvc.get("metadata") or {}).get("labels") or {}).get(PREVIOUS) != "true":
        raise ValueError(f"{ns}/{name} is not a previous volume")
    return ns, name, pvc


def keep(body, role):
    """Keep stops the expiry (operator: it only prevents a deletion); un-keep
    starts it again (admin: it re-arms one). Either way a fresh reminder comes
    a day before it goes."""
    want = (body or {}).get("keep")
    if not isinstance(want, bool):
        raise ValueError("keep must be true or false")
    if not want and role != "admin":
        raise PermissionError("letting a previous volume expire again needs the admin role")
    ns, name, _ = _previous_claim(body)
    ksend("PATCH", _pvc_path(ns, name), {"metadata": {"annotations": {KEEP: "true" if want else None, REMINDED: None}}},
          ctype="application/merge-patch+json")
    return {**_row(kget(_pvc_path(ns, name)), keep_days()), "detail": (
        f"{name} is kept until someone deletes it" if want else f"{name} expires again, with a reminder the day before")}


def swap_back(body):
    """Point the app back at a previous volume: the same job, the other way
    round; the volume it leaves becomes the previous one."""
    b = body or {}
    with OPS.transaction():
        ns, name, pvc = _previous_claim(b)
        notes = (pvc.get("metadata") or {}).get("annotations") or {}
        app, current = _of(notes), notes.get(NAMES.key("replaced-by"), "")
        dep = KUBE.get_optional(_dep_path(ns, _valid(app, "app")), read=kget)
        if not dep:
            raise ValueError(f"{app} no longer exists in {ns}; nothing to swap back")
        indexes = _indexes(dep, current)
        if not indexes:
            raise ValueError(f"{app} no longer uses {current}; point it at {name} with Edit instead")
        digest = _back_digest(ns, app, current, name, (pvc.get("metadata") or {}).get("uid", ""), dep)
        if str(b.get("digest") or "") != digest:
            return MODULE_ROUTES.Reply(409, {"error": f"{app} or {name} changed since this was shown; check it again",
                                             "volume": _row(pvc, keep_days(), dep=dep)})
        if _pods_using(ns, name):
            raise ValueError(f"{name} is in use; it cannot be swapped in")
        busy = _busy(OPS.snapshot(), ns, app, (current, name))
        if busy:
            raise ValueError(f"{app} already has a job running ({busy.get('title') or busy.get('kind')}); let it finish first")
        leaving = KUBE.get_optional(_pvc_path(ns, current), read=kget) or {}
        ref = {"namespace": ns, "app": app, "kind": "Deployment", "volume": _volumes(dep)[indexes[0]]["name"],
               "indexes": indexes, "old_claim": current, "new_claim": name,
               "old_uid": (leaving.get("metadata") or {}).get("uid", ""), "new_uid": (pvc.get("metadata") or {}).get("uid", ""),
               "old_volume": _handle(leaving) if leaving else "", "backup": "",
               "generation": (dep.get("metadata") or {}).get("generation"),
               "direction": "back", "phase": "stopping", "since": time.time()}
        operation = OPS.start(KIND, f"Swap {app} back to {name}", {"kind": "Deployment", "name": app, "namespace": ns},
                              "/data-protection", ref, f"Stopping {app}")
    return {"ok": True, "operation": operation, "detail": f"Swapping {app} back to {name}"}


def set_keep_days(value):
    if isinstance(value, bool):
        raise ValueError("keep days must be a whole number")
    try:
        days = int(value)
    except (TypeError, ValueError):
        raise ValueError("keep days must be a whole number") from None
    if days < 0 or days > 90:
        raise ValueError("previous volumes are kept 1 to 90 days, or 0 to keep them until deleted")
    save_settings({"restore_swap": {"keep_days": days}})
    return {"ok": True, "keep_days": days,
            "detail": f"Previous volumes are kept {days} days" if days else "Previous volumes are kept until deleted"}


# ---- the expiry: on the leader, hourly ---------------------------------------------

def _held(item, ns, name):
    ref, resource = item.get("ref") or {}, item.get("resource") or {}
    return (ref.get("namespace") or resource.get("namespace")) == ns and name in (_names_in(ref) | {resource.get("name")})


def _expire_one(pvc, days, active, now):
    meta = pvc.get("metadata") or {}
    notes = meta.get("annotations") or {}
    ns, name, app = meta.get("namespace", ""), meta.get("name", ""), _of(notes)
    if notes.get(KEEP) == "true":
        return None
    review = deletion_plan(ns, name)
    users = [c for c in review.get("consumers") or [] if c.get("active") or c.get("kind") not in ("Job", "Pod")]
    if users:
        # Swapped back by hand, or put back by Flux: no longer a copy nobody uses.
        _unmark(ns, name)
        note({"key": f"previous-volume:{ns}/{name}:in-use:{int(now)}", "category": "degraded", "severity": "info",
              "event": True, "title": f"{app} uses {name} again; it is no longer kept for deletion",
              "body": f"{users[0]['kind']} {users[0]['name']} mounts it, so it is not a previous volume any more.",
              "href": "/data-protection"})
        return ("in-use", ns, name)
    replaced = _number(notes, NAMES.key("replaced-at"))
    if not replaced:
        return None
    expires, reminded = replaced + days * DAY, _number(notes, REMINDED)
    if not reminded:
        if now >= expires - DAY:
            # Every deletion is announced a day ahead, even after a shorter
            # setting or a cluster that was down past the date.
            ksend("PATCH", _pvc_path(ns, name), {"metadata": {"annotations": {REMINDED: str(int(now))}}},
                  ctype="application/merge-patch+json")
            return ("reminded", ns, name)
        return None
    if now < expires or now - reminded < DAY:
        return None
    if meta.get("uid") != notes.get(NAMES.key("replaced-uid")):
        return None                       # a claim made again under the name is someone's data
    if any(_held(item, ns, name) for item in active):
        return None
    if ns in system_namespaces() or review.get("blocking_reasons"):
        return None
    delete_volume({"namespace": ns, "name": name, "action": "delete_data", "uid": meta["uid"], "confirmation": name})
    note({"key": f"previous-volume:{ns}/{name}:deleted:{int(now)}", "category": "degraded", "severity": "info",
          "event": True, "title": f"Deleted {app}'s previous volume {name} (kept {days} days)",
          "body": f"{app} runs on {notes.get(NAMES.key('replaced-by'), 'its restored volume')}. "
                  "Backups of either volume are not touched.", "href": "/data-protection"})
    return ("deleted", ns, name)


def expire_tick(now=None):
    """Remind a day ahead, then delete, each previous volume past its time.
    Any doubt about one claim skips it until the next hour."""
    now = now or time.time()
    if not is_leader():
        return []
    days = keep_days()
    if not days:
        return []
    active = [item for item in OPS.snapshot() if item.get("status") not in TERMINAL]
    done = []
    for pvc in _labelled():
        try:
            outcome = _expire_one(pvc, days, active, now)
        except Exception as error:
            print(f"previous volumes: {(pvc.get('metadata') or {}).get('name')}: {str(error)[:160]}", flush=True)
            continue
        if outcome:
            done.append(outcome)
    return done


def alert_facts(now=None):
    """A reminded previous volume no one chose to keep: one info alert each."""
    now = now or time.time()
    days = keep_days()
    if not days:
        return []
    facts = []
    for pvc in _labelled():
        meta = pvc.get("metadata") or {}
        notes = meta.get("annotations") or {}
        if notes.get(KEEP) == "true" or not _number(notes, REMINDED):
            continue
        ns, name, app, due = meta.get("namespace", ""), meta.get("name", ""), _of(notes), _due(notes, days)
        when = ("soon" if due <= now else "tomorrow" if due - now <= 1.5 * DAY else
                "on " + datetime.datetime.fromtimestamp(due, datetime.timezone.utc).strftime("%-d %b"))
        facts.append({"key": f"previous-volume:{ns}/{name}", "category": "degraded", "severity": "info",
                      "title": f"{app}'s previous volume {name} will be deleted {when}",
                      "resolved": f"{app}'s previous volume {name} is no longer due for deletion",
                      "body": "Restore into this app moved it off this volume. Keep it under Data protection, "
                              "Previous volumes, to stop the deletion.",
                      "href": "/data-protection?" + urllib.parse.urlencode({"previous": f"{ns}/{name}"})})
    return facts


def _plan_route(request):
    q = lambda name: (request.query.get(name) or [""])[0]
    return public(plan(q("backup"), q("ns"), q("app"), q("claim"), q("new_name")))


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/restore-swap/plan"): ("viewer", _plan_route),
    ("POST", "/api/restore-swap"): ("admin", lambda request: start(request.body)),
    ("GET", "/api/volumes/previous"): ("viewer", lambda request: previous_volumes()),
    ("POST", "/api/volumes/previous/keep"): ("operator", lambda request: keep(request.body, request.role)),
    ("POST", "/api/volumes/previous/swap-back"): ("admin", lambda request: swap_back(request.body)),
    ("POST", "/api/volumes/previous/settings"): ("admin", lambda request: set_keep_days((request.body or {}).get("keep_days"))),
}


def register(ops):
    """The job store's entry for a restore into an app (#743); called from
    homestead_storage_server.register, so server.py does not change."""
    ops.RESOLVERS[KIND] = lambda item: resolve(item, ops.checkpoint)
    ops.CANCELLERS[KIND] = (cancel_plan, cancel_run)
    # No OPS.CONFLICTS rule: start and swap_back check _busy and start the
    # job inside one OPS.transaction, so two cannot pass the check together.
