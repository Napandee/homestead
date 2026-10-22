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
import socket
import time
import urllib.error

KIND = "weekly-trim"
STATE = "weekly-trim.json"
# Saturdays 04:00 UTC: Longhorn's TRIM_CRON ("0 4 * * 6"), which the
# RecurringJob Homestead used to make ran on.
SLOT_WEEKDAY, SLOT_HOUR = 5, 4
WEEK = 7 * 86400
VM_DISK = "no filesystem (VM disk)"
DETACHED_ANSWER = "Action [trimFilesystem] not available"
BLOCK_ANSWER = "with Block VolumeMode"


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
