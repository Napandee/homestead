"""
Homestead's weekly trim (#591): once a week Homestead trims every attached
Longhorn volume that has a filesystem, itself, as a job that says what it
trimmed and what it skipped.

Deleted files leave their blocks allocated in a Longhorn volume until its
filesystem is trimmed. Longhorn's own filesystem-trim RecurringJob tries
every volume in its groups and fails on each detached volume and each VM
disk (a Block volume has no filesystem to trim), so it failed every week
and said nothing useful. Homestead now picks the volumes itself: attached
ones with a filesystem are trimmed, one per step of the job; the others are
skipped and named. The result is kept in DATA_DIR/weekly-trim.json under
"last".
"""
import os
import socket
import time

import homestead_longhorn as LH
import homestead_longhorn_jobs as LH_JOBS
import homestead_names as NAMES
import homestead_operations as OPS
import homestead_shared as SHARED

KIND = "weekly-trim"
STATE = "weekly-trim.json"
# Saturdays 04:00 UTC: Longhorn's TRIM_CRON ("0 4 * * 6"), which the
# RecurringJob Homestead used to make ran on.
SLOT_WEEKDAY, SLOT_HOUR = 5, 4
WEEK = 7 * 86400
VM_DISK = "no filesystem (VM disk)"
DETACHED_ANSWER = "Action [trimFilesystem] not available"
BLOCK_ANSWER = "with Block VolumeMode"
DATA_DIR = None


def last_slot(now):
    """The most recent Saturday 04:00 UTC at or before now."""
    day = int(now) // 86400
    weekday = (day + 3) % 7                 # 1 January 1970 was a Thursday
    slot = (day - (weekday - SLOT_WEEKDAY) % 7) * 86400 + SLOT_HOUR * 3600
    return slot if slot <= now else slot - WEEK


def due(state, now):
    """Switched on and not yet run, nor tried, for this week's slot. A slot
    missed while Homestead was down runs once, late."""
    if state.get("enabled") is False:
        return False
    slot = last_slot(now)
    return (state.get("last") or {}).get("slot") != slot and state.get("tried") != slot


def _row(volume, why):
    k8s = (volume.get("status") or {}).get("kubernetesStatus") or {}
    return {"volume": volume["metadata"]["name"], "pvc": k8s.get("pvcName") or "", "why": why}


def plan(volumes, pvs):
    """Which volumes a run trims, and which it skips and why. volumes are
    Longhorn's volume items; pvs maps a PV's name to its volumeMode. A volume
    with no PV (no claim) counts as a filesystem."""
    trim, skipped = [], []
    for volume in sorted(volumes, key=lambda v: v["metadata"]["name"]):
        status = volume.get("status") or {}
        state = status.get("state") or ""
        pv = (status.get("kubernetesStatus") or {}).get("pvName") or volume["metadata"]["name"]
        if state != "attached":
            skipped.append(_row(volume, state or "not attached"))
        elif pvs.get(pv) == "Block":
            skipped.append(_row(volume, VM_DISK))
        else:
            trim.append(volume["metadata"]["name"])
    return {"trim": trim, "skipped": skipped}


def _timed_out(error):
    reason = getattr(error, "reason", None)
    return isinstance(error, (socket.timeout, TimeoutError)) or isinstance(reason, (socket.timeout, TimeoutError))


def classify(error):
    """What Longhorn's answer to one trim means: (outcome, why). A volume
    that detached since the run read the list is skipped, as is a VM disk;
    no answer within the backend call's 60 s is not confirmed, not failed."""
    if error is None:
        return "trimmed", ""
    message = str(error)
    if DETACHED_ANSWER in message:
        return "skipped", "detached"
    if BLOCK_ANSWER in message:
        return "skipped", VM_DISK
    if _timed_out(error):
        return "unconfirmed", "Longhorn did not answer within 60 s"
    return "failed", message[:300]


def bind(data_dir):
    global DATA_DIR
    DATA_DIR = data_dir


def _path():
    return os.path.join(DATA_DIR, STATE)


def _update(change):
    """Change the saved state in place and write it back; the rest of it
    (created, groups, enabled, retired) is kept. An unreadable file raises,
    and nothing is written over it."""
    state = SHARED.read_json(_path(), {})
    change(state)
    SHARED.write_json(_path(), state, durable=True)
    return state


def start(why="weekly"):
    """Start one weekly trim as a job; one runs at a time."""
    slot = last_slot(time.time())
    operation = OPS.start(KIND, "Weekly trim", {"kind": "Longhorn", "name": "weekly trim"},
                          "/data-protection#housekeeping", {"phase": "plan", "why": why, "slot": slot},
                          "Finding attached volumes", unless=(KIND,))
    _update(lambda state: state.update(tried=slot))
    return operation


def _name(ref, volume):
    return (ref.get("pvcs") or {}).get(volume) or volume


def _volumes_and_modes():
    """Longhorn's volumes, read directly so that an error is one (LH's own
    volume list answers [] when it cannot read them), and each PV's
    volumeMode. Without the PVs a VM disk still reaches Longhorn, whose
    answer classify() reads as a skip."""
    volumes = LH.kget(f"{LH.API}/namespaces/{LH.LHNS}/volumes").get("items", [])
    try:
        pvs = LH.kget("/api/v1/persistentvolumes").get("items", [])
    except Exception:
        pvs = []
    return volumes, {p["metadata"]["name"]: (p.get("spec") or {}).get("volumeMode") or "Filesystem" for p in pvs}


def resolve(item, checkpoint):
    """Move the weekly trim on one step: (status, progress, message). The
    plan step reads the volumes; each later step trims one, records what
    Longhorn said and checkpoints, so a run that a restart or a new leader
    picks up carries on from the next volume (a trim done twice is harmless)."""
    ref = item["ref"]
    if ref.get("phase", "plan") == "plan":
        try:
            volumes, modes = _volumes_and_modes()
        except Exception as error:
            return "failed", 0, f"Longhorn's volumes could not be read: {str(error)[:200]}"
        chosen = plan(volumes, modes)
        pvcs = {v["metadata"]["name"]: ((v.get("status") or {}).get("kubernetesStatus") or {}).get("pvcName") or ""
                for v in volumes}
        ref.update(phase="trim", todo=chosen["trim"], done=[], skipped=chosen["skipped"],
                   pvcs={name: pvc for name, pvc in pvcs.items() if pvc})
        checkpoint(item)
        return "running", 1, f"Trimming {len(chosen['trim'])} attached volumes, one at a time"
    todo, done = ref.get("todo") or [], ref.setdefault("done", [])
    if todo:
        volume = todo[0]
        try:
            LH_JOBS._backend("POST", f"/volumes/{volume}?action=trimFilesystem", {})
            error = None
        except Exception as caught:
            error = caught
        outcome, why = classify(error)
        done.append({"volume": volume, "outcome": outcome, "why": why})
        ref["todo"] = todo[1:]
        checkpoint(item)
        total = len(done) + len(ref["todo"])
        if ref["todo"]:
            return "running", max(1, min(99, round(100 * len(done) / total))), \
                f"Trimmed {_name(ref, volume)} ({len(done)} of {total})" if outcome == "trimmed" else \
                f"{_name(ref, volume)}: {outcome} ({len(done)} of {total})"
    return _finish(ref)


def _count(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def _finish(ref):
    """The run's result: saved as state["last"], and said in one message."""
    row = lambda d: {"volume": d["volume"], "pvc": (ref.get("pvcs") or {}).get(d["volume"], "")}
    done = ref.get("done") or []
    last = {"slot": ref.get("slot"), "at": round(time.time()), "why": ref.get("why", ""),
            "trimmed": [d["volume"] for d in done if d["outcome"] == "trimmed"],
            "skipped": list(ref.get("skipped") or []) + [{**row(d), "why": d["why"]} for d in done
                                                          if d["outcome"] == "skipped"],
            "unconfirmed": [{**row(d), "why": d["why"]} for d in done if d["outcome"] == "unconfirmed"],
            "failed": [{**row(d), "message": d["why"]} for d in done if d["outcome"] == "failed"]}
    _update(lambda state: state.update(last=last))
    name = lambda r: r["pvc"] or r["volume"]
    whys = {}
    for s in last["skipped"]:
        whys[s["why"]] = whys.get(s["why"], 0) + 1
    said = ", ".join(_count(n, "VM disk") if why == VM_DISK else f"{n} {why}" for why, n in whys.items())
    message = (f"Trimmed {_count(len(last['trimmed']), 'volume')}, skipped {len(last['skipped'])}"
               + (f" ({said})" if said else ""))
    # Failures first: the job's message is cut at 500 characters, the saved result is not.
    if last["failed"]:
        message += ". Failed: " + "; ".join(f"{name(f)}: {f['message']}" for f in last["failed"])
    if last["unconfirmed"]:
        message += ". Not confirmed: " + ", ".join(f"{name(u)} ({u['why']})" for u in last["unconfirmed"])
    if last["skipped"]:
        message += ". Skipped: " + ", ".join(name(s) for s in last["skipped"])
    return ("failed" if last["failed"] else "succeeded"), 100, message


def _as_homestead_left_it(job, state):
    """Homestead's old trim job, exactly as Homestead last wrote it: its
    schedule, its groups and its label. Changed in any of these, it is the
    user's (state["groups"] is None once someone chose its groups)."""
    spec, labels = job.get("spec") or {}, (job.get("metadata") or {}).get("labels") or {}
    if labels.get(NAMES.key("managed")) != "true" or spec.get("task") != "filesystem-trim" \
            or spec.get("cron") != LH_JOBS.TRIM_CRON:
        return False
    if "groups" not in state:
        return True
    return state["groups"] is not None and sorted(spec.get("groups") or []) == sorted(state["groups"])


def _retire(jobs, state):
    """Once: remove the Longhorn trim job Homestead made, unless someone
    changed it (theirs, so Homestead's own run stays off) or deleted it
    (they did not want a trim, so it stays off too). Returns (what changed,
    whether it was removed)."""
    ours = next((j for j in jobs if j["metadata"]["name"] == LH_JOBS.TRIM_JOB), None)
    if ours is not None and _as_homestead_left_it(ours, state):
        LH_JOBS.delete_job(LH_JOBS.TRIM_JOB)
        state.update(retired=True, enabled=True)
        return f"Longhorn trim job {LH_JOBS.TRIM_JOB} removed: Homestead trims attached volumes itself", True
    if ours is not None:
        state.update(retired=True, enabled=False)
        return f"Longhorn trim job {LH_JOBS.TRIM_JOB} was changed, so it is kept as yours; Homestead's own trim is off", False
    state.update(retired=True, enabled=False if state.get("created") else state.get("enabled", True))
    return "", False


def theirs(jobs):
    """A trim job of the user's that covers the default group: while one is
    there Homestead does not trim as well."""
    return next((j["metadata"]["name"] for j in jobs if (j.get("spec") or {}).get("task") == "filesystem-trim"
                 and "default" in ((j.get("spec") or {}).get("groups") or [])), "")


def tick(now, operations):
    """On the leader, every ten minutes: retire the old trim job once, then
    start this week's run when it is due. Returns what changed, or ""."""
    try:
        jobs = LH.kget(f"{LH.API}/namespaces/{LH.LHNS}/recurringjobs").get("items", [])
        state = SHARED.read_json(_path(), {})
    except Exception as error:
        return f"weekly trim not checked: {str(error)[:160]}"
    said = []
    if not state.get("retired"):
        change, removed = _retire(jobs, state)
        SHARED.write_json(_path(), state, durable=True)
        said.append(change)
        if removed:
            jobs = [j for j in jobs if j["metadata"]["name"] != LH_JOBS.TRIM_JOB]
    busy = any(o.get("kind") == KIND and o.get("status") in ("queued", "running") for o in operations)
    if due(state, now) and not busy and not theirs(jobs):
        start("weekly")
        said.append("weekly trim started")
    return "; ".join(s for s in said if s)


def _running():
    return any(o.get("kind") == KIND and o.get("status") in ("queued", "running") for o in OPS.snapshot())


def view():
    """Data protection's Housekeeping card: on or off, the next run, the
    last result, and a trim job of the user's that keeps ours from running."""
    state = SHARED.read_json(_path(), {})
    enabled = state.get("enabled", True) is not False
    try:
        mine = theirs(LH.kget(f"{LH.API}/namespaces/{LH.LHNS}/recurringjobs").get("items", []))
    except Exception:
        mine = ""
    upcoming = last_slot(time.time()) + WEEK
    return {"enabled": enabled, "next": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(upcoming)) if enabled else "",
            "last": state.get("last"), "running": _running(), "theirs": mine}


def run_now():
    """Trim every attached volume now; one run at a time."""
    if _running():
        raise ValueError("a weekly trim is already running; one runs at a time")
    return {"ok": True, "operation": start("Run now")}


def switch(enabled):
    """Homestead's weekly trim on or off."""
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be true or false")
    _update(lambda state: state.update(enabled=enabled))
    return {"ok": True, "enabled": enabled,
            "detail": "Attached volumes are trimmed every Saturday at 04:00 UTC" if enabled
            else "The weekly trim is off; Run now still trims every attached volume"}


# Its routes and who may use them (homestead_routes.py): trimming is an
# operator's, as Trim now is; switching it off is an admin's.
ROUTES = {
    ("GET", "/api/weekly-trim"): ("viewer", lambda request: view()),
    ("POST", "/api/weekly-trim/run"): ("operator", lambda request: run_now()),
    ("POST", "/api/weekly-trim/settings"): ("admin", lambda request: switch(request.body.get("enabled"))),
}


def register(ops):
    """The job store's entry for the weekly trim (#622)."""
    ops.RESOLVERS[KIND] = lambda item: resolve(item, ops.checkpoint)
