"""Rolling a volume back to one of its snapshots.

Longhorn reverts only a volume nothing is using, attached to a node with no
block device on it (maintenance mode), and that is only done through its own
API. So, step by step, each advanced by the operations poll:

  1. what mounts the claim is stopped, and how each ran is recorded - as a
     storage class change does;
  2. once the volume has detached, it is attached in maintenance mode;
  3. the state it is in now is kept as a snapshot of its own, so the
     rollback can itself be undone by rolling back to that;
  4. the volume is reverted to the chosen snapshot;
  5. it is detached, and everything is started again the way it was.

A failure before the revert puts things back: the volume is detached and
everything started again, unchanged. So does a failure of Longhorn's API up
to the revert: a connection that fails is tried again a few times, then the
job ends rather than keep the app stopped. A revert whose answer is lost may
still be running in Longhorn: the volume is held until Longhorn's engine
shows whether it happened, and if that cannot be told it stays held and the
app stopped, saying so. Before anything is stopped, Longhorn's API must answer.
"""
import json
import time
import urllib.error
import urllib.request

import homestead_kube as KUBE
import homestead_longhorn_access as ACCESS
import homestead_operations as OPS
import homestead_self as SELF
import homestead_storage_guard as STORAGE_GUARD

LHNS = "longhorn-system"
API = f"/apis/longhorn.io/v1beta2/namespaces/{LHNS}"
BACKEND = ACCESS.URL
ATTACHMENT = "homestead-revert"
WAIT = {"stopping": 600, "attaching": 300, "confirming": 300, "detaching": 300, "starting": 600}
# A step whose connection failed (refused, reset, timed out) is tried again on
# a later round, this far apart, this many times in all; longhorn-backend
# spreads requests over the manager pods, so one can blip.
TRIES, RETRY_AFTER = 3, 5


class Refused(ValueError):
    """Longhorn answered, and turned the request down."""

kget = ksend = None
reclass = None          # homestead_reclass, for what uses a claim
is_self = lambda ns, name: False
lh_call = None          # (method, path, body) -> dict, Longhorn's own API


def bind(_kget, _ksend, _reclass, _is_self=None, _lh_call=None):
    global kget, ksend, reclass, is_self, lh_call
    kget, ksend, reclass = _kget, _ksend, _reclass
    is_self = _is_self or is_self
    lh_call = _lh_call or _backend


def _backend(method, path, body=None):
    data = json.dumps(body or {}).encode()
    request = urllib.request.Request(f"{BACKEND}{path}", data=data if method == "POST" else None, method=method,
                                     headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            text = response.read().decode() or "{}"
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:300]
        raise Refused(f"Longhorn refused it: {detail or error.reason}") from error
    return json.loads(text)


def _volume(name):
    volume = KUBE.get_optional(f"{API}/volumes/{name}", read=kget)
    if not volume:
        raise ValueError(f"there is no Longhorn volume {name}")
    return volume


def _snapshot(name, volume):
    snap = KUBE.get_optional(f"{API}/snapshots/{name}", read=kget)
    if not snap or (snap.get("spec") or {}).get("volume") != volume:
        raise ValueError(f"{volume} has no snapshot {name}")
    return snap


def _replica_node(volume):
    """A node holding a working copy, to attach the volume to."""
    for replica in (kget(f"{API}/replicas").get("items") or []):
        spec, status = replica.get("spec") or {}, replica.get("status") or {}
        if spec.get("volumeName") == volume and spec.get("nodeID") and not spec.get("failedAt") \
                and status.get("currentState") in ("running", "stopped", ""):
            return spec["nodeID"]
    return ""


def plan(volume, snapshot):
    """What rolling back would stop, before anything is changed."""
    vol = _volume(volume)
    snap = _snapshot(snapshot, volume)
    k8s = (vol.get("status") or {}).get("kubernetesStatus") or {}
    ns, claim = k8s.get("namespace", ""), k8s.get("pvcName", "")
    users = reclass.consumers(ns, claim) if ns and claim else []
    blockers = []
    for user in users:
        if user["kind"] == "DaemonSet":
            blockers.append(f"{user['name']} is a DaemonSet, which cannot be stopped for this; remove its claim first")
        if is_self(ns, user["name"]):
            blockers.append("this is Homestead's own data: rolling it back would stop Homestead part-way")
    known = {user["name"] for user in users}
    loose = [pod["metadata"]["name"] for pod in (reclass._using_pods(ns, claim, helpers=False) if claim else [])
             if not any(pod["metadata"]["name"].startswith(name) for name in known)]
    if loose:
        blockers.append(f"{', '.join(loose[:3])} use{'s' if len(loose) == 1 else ''} it and belong{'s' if len(loose) == 1 else ''} "
                        "to nothing Homestead can stop and start again; stop it first")
    status = snap.get("status") or {}
    return {"volume": volume, "snapshot": snapshot, "namespace": ns, "claim": claim,
            "created": status.get("creationTime", ""), "consumers": users, "blockers": blockers,
            "ready": not blockers and bool(status.get("readyToUse", True))}


def start(volume, snapshot, ops):
    p = plan(volume, snapshot)
    if p["blockers"]:
        raise ValueError(p["blockers"][0])
    # Before anything is stopped: every later step goes through Longhorn's API.
    ACCESS.require(kget, ksend, SELF.NS, f"Rolling {p['claim'] or volume} back")
    ref = {"volume": volume, "snapshot": snapshot, "namespace": p["namespace"], "claim": p["claim"],
           "consumers": p["consumers"], "phase": "stopping", "since": time.time()}
    label = p["claim"] or volume
    return ops.start("snapshot-revert", f"Roll {label} back to a snapshot",
                     {"kind": "Volume", "name": label, "namespace": p["namespace"]}, "/data-protection", ref,
                     f"Stopping what uses {label}")


def _stop(ns, ref):
    for c in ref["consumers"]:
        if c.get("stopped"):
            continue
        name = c["name"]
        if c["kind"] in ("Deployment", "StatefulSet"):
            plural = "deployments" if c["kind"] == "Deployment" else "statefulsets"
            ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/{plural}/{name}", {"spec": {"replicas": 0}},
                  ctype="application/merge-patch+json")
        elif c["kind"] == "CronJob":
            ksend("PATCH", f"/apis/batch/v1/namespaces/{ns}/cronjobs/{name}", {"spec": {"suspend": True}},
                  ctype="application/merge-patch+json")
        elif c["kind"] == "VirtualMachine":
            ksend("PATCH", f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}",
                  {"spec": {"runStrategy": "Halted", "running": None}}, ctype="application/merge-patch+json")
        c["stopped"] = True


def _phase(ref, phase):
    ref["phase"], ref["since"] = phase, time.time()


def _late(ref):
    return time.time() - float(ref.get("since") or 0) > WAIT.get(ref.get("phase"), 600)


def _state(volume):
    return ((_volume(volume).get("status") or {}).get("state") or "").lower()


def _why(error):
    return str(getattr(error, "reason", None) or error)


def _connection(error):
    """A failure on the connection - refused, reset, timed out - rather than
    an answer: the request may be tried again."""
    return isinstance(error, OSError) and not isinstance(error, urllib.error.HTTPError)


def _never_sent(error):
    """Whether a failed request certainly changed nothing: Longhorn turned it
    down, or the connection was refused before it went out."""
    return isinstance(error, Refused) or isinstance(getattr(error, "reason", error), ConnectionRefusedError)


def _again(ref, step, error=None):
    """Whether a failed step is tried again on a later round."""
    if error is not None and not _connection(error):
        return False
    tries = ref.setdefault("tries", {})
    tries[step] = tries.get(step, 0) + 1
    if tries[step] >= TRIES:
        return False
    ref["retry_at"] = time.time() + RETRY_AFTER
    return True


def _pausing(ref):
    return time.time() < float(ref.get("retry_at") or 0)


def _drop_ticket(ref):
    """Remove the rollback's attachment ticket through Kubernetes, not
    Longhorn's API: "" once it is gone, else why not."""
    try:
        ksend("PATCH", f"{API}/volumeattachments/{ref['volume']}", {"spec": {"attachmentTickets": {ATTACHMENT: None}}},
              ctype="application/merge-patch+json")
    except urllib.error.HTTPError as error:
        if error.code != 404:                       # no attachment record: nothing holds it
            return _why(error)
    except Exception as error:
        return _why(error)
    ref["attached"] = False
    return ""


def _let_go(ref):
    """Detach the volume from the rollback; "" once done, else why not."""
    try:
        lh_call("POST", f"/volumes/{ref['volume']}?action=detach",
                {"attachmentID": ATTACHMENT, "hostId": ref.get("node", ""), "forceDetach": False})
    except Exception as error:
        dropped = _drop_ticket(ref)
        return f"{_why(error)}; {dropped}" if dropped else ""
    ref["attached"] = False
    return ""


def _held(ref, stuck):
    return (f". Homestead could not let go of {ref['volume']} ({stuck}): until the Longhorn attachment ticket "
            f"{ATTACHMENT} on it is removed, its app cannot start (see What Homestead changes)") if stuck else ""


def _names(ref):
    return ", ".join(c["name"] for c in ref.get("consumers") or []) or "what used it"


def _put_back(ref, why, stuck=""):
    """Before the revert, or once it is known not to have happened: let go of
    the volume, start everything again, and say why - and what may have changed."""
    started = "everything that was running is starting again"
    try:
        if ref.get("attached"):
            stuck = _let_go(ref)
    finally:
        try:
            if ref.get("namespace"):
                reclass._start(ref["namespace"], ref)
        except Exception as error:
            started = f"Homestead could not start {_names(ref)} again ({_why(error)}): start them yourself"
    if ref.get("reverting"):
        changed = (f"Longhorn may have rolled it back before failing: the state before is kept as snapshot "
                   f"{ref.get('kept', '')}, to roll back to if needed")
    elif ref.get("kept"):
        changed = f"Nothing was rolled back; the state it is in is also kept as snapshot {ref['kept']}"
    else:
        changed = "Nothing was changed"
    return "failed", 100, f"{why}. {changed}; {started}{_held(ref, stuck)}"


def _attach(ref, volume):
    lh_call("POST", f"/volumes/{volume}?action=attach",
            {"hostId": ref["node"], "disableFrontend": True, "attachedBy": "", "attacherType": "longhorn-api",
             "attachmentID": ATTACHMENT})


def _snapshot_made(volume, name):
    """Whether Longhorn made a snapshot whose request failed on the way back."""
    try:
        snap = KUBE.get_optional(f"{API}/snapshots/{name}", read=kget)
    except Exception:
        return False
    return bool(snap) and (snap.get("spec") or {}).get("volume") == volume


def _head_parent(volume):
    """The snapshot the volume's live data sits on, from Longhorn's engine:
    the rolled-back-to snapshot once a revert is done. "" when unknown."""
    try:
        engines = kget(f"{API}/engines?labelSelector=longhornvolume%3D{volume}").get("items") or []
    except Exception:
        return ""
    for engine in engines:
        head = (((engine.get("status") or {}).get("snapshots") or {}).get("volume-head") or {})
        if head.get("parent"):
            return head["parent"]
    return ""


def _reverted(ref):
    ref["reverted"] = True
    _phase(ref, "detaching")
    if _let_go(ref):                    # tried again while detaching
        ref.setdefault("tries", {})["detach"] = 1
        ref["retry_at"] = time.time() + RETRY_AFTER
    return "running", 70, f"Rolled back to {ref['snapshot']}; the state before is kept as {ref.get('kept', '')}"


def resolve(item):
    ref = item["ref"]
    ns, volume = ref.get("namespace", ""), ref["volume"]
    phase = ref.get("phase")
    if phase == "stopping":
        if ns:
            _stop(ns, ref)
        using = reclass._using_pods(ns, ref["claim"], helpers=True) if ns and ref.get("claim") else []
        if using or _state(volume) != "detached":
            if _late(ref):
                return _put_back(ref, f"{volume} did not detach in time"
                                      + (f" - {using[0]['metadata']['name']} still uses it" if using else ""))
            return "running", 10, f"Waiting for {ref['claim'] or volume} to be let go"
        node = _replica_node(volume)
        if not node:
            return _put_back(ref, f"no node holds a working copy of {volume}")
        ref["node"] = node
        try:
            _attach(ref, volume)
        except Exception as error:
            if _again(ref, "attach", error):
                # Longhorn may hold it already; the same ticket is asked for again.
                ref.update(attached=True, reattach=True)
                _phase(ref, "attaching")
                return "running", 25, f"Longhorn's API did not answer ({_why(error)}); trying again"
            # Longhorn may have taken the ticket before failing: drop it
            # through Kubernetes, which needs no answer from Longhorn's API.
            return _put_back(ref, f"Longhorn's API did not attach {volume} for the rollback: {_why(error)}",
                             _drop_ticket(ref))
        ref["attached"] = True
        _phase(ref, "attaching")
        return "running", 30, f"Attaching {volume} to {node} for the rollback"
    if phase == "attaching":
        if _pausing(ref):
            return "running", 30, f"Trying Longhorn's API again for {volume}"
        if ref.get("reattach"):
            try:
                _attach(ref, volume)
            except Exception as error:
                if _again(ref, "attach", error):
                    return "running", 25, f"Longhorn's API did not answer ({_why(error)}); trying again"
                return _put_back(ref, f"Longhorn's API did not attach {volume} for the rollback: {_why(error)}")
            ref["reattach"] = False
        if _state(volume) != "attached":
            if _late(ref):
                return _put_back(ref, f"{volume} did not attach for the rollback")
            return "running", 35, f"Attaching {volume} to {ref.get('node')}"
        if not ref.get("kept"):
            keep = ref.setdefault("keep", f"before-rollback-{int(time.time())}")
            try:
                lh_call("POST", f"/volumes/{volume}?action=snapshotCreate", {"name": keep})
            except Exception as error:
                if not _snapshot_made(volume, keep):
                    if _again(ref, "snapshotCreate", error):
                        return "running", 40, f"Longhorn's API did not answer ({_why(error)}); trying again"
                    return _put_back(ref, f"Longhorn's API did not keep the present state of {volume}: {_why(error)}")
            ref["kept"] = keep
        ref["reverting"] = True
        try:
            lh_call("POST", f"/volumes/{volume}?action=snapshotRevert", {"name": ref["snapshot"]})
        except Exception as error:
            if _never_sent(error):
                ref["reverting"] = False
                return _put_back(ref, f"Longhorn did not roll {volume} back: {_why(error)}")
            # The request went out: Longhorn may still be reverting. Never let
            # go of the volume, or start what uses it, until that is known.
            ref["revert_error"] = _why(error)
            _phase(ref, "confirming")
            return "running", 60, f"Longhorn's API did not answer the rollback ({_why(error)}); checking whether it happened"
        return _reverted(ref)
    if phase == "confirming":
        parent = _head_parent(volume)
        if parent == ref["snapshot"]:
            return _reverted(ref)
        if not _late(ref):
            return "running", 65, f"Checking whether Longhorn rolled {volume} back"
        why = f"Longhorn's API did not answer the rollback ({ref.get('revert_error', '')})"
        if parent and parent == ref.get("kept"):
            ref["reverting"] = False
            return _put_back(ref, f"{why}, and Longhorn did not roll {volume} back")
        return ("failed", 100, f"{why}, and whether {volume} was rolled back cannot be told. To keep its data "
                               f"consistent it stays attached in maintenance mode and {_names(ref)} stay stopped. "
                               f"Check its snapshots in Longhorn; the state before is kept as snapshot "
                               f"{ref.get('kept', '')}. Then remove the Longhorn attachment ticket {ATTACHMENT} on "
                               f"it (see What Homestead changes) and start {_names(ref)} again")
    if phase == "detaching":
        if ref.get("attached"):
            if _pausing(ref) and not _late(ref):
                return "running", 75, f"Letting go of {volume}"
            stuck = _let_go(ref)
            if stuck and _again(ref, "detach") and not _late(ref):
                return "running", 75, f"Letting go of {volume}: {stuck}"
            ref["stuck"] = stuck
        if _state(volume) != "detached" and not _late(ref):
            return "running", 80, f"Detaching {volume}"
        if ns:
            reclass._start(ns, ref)
        _phase(ref, "starting")
        return "running", 85, "Starting everything again"
    if phase == "starting":
        waiting = reclass._started(ns, ref) if ns else []
        if waiting and not _late(ref):
            return "running", 90, f"Waiting for {', '.join(waiting[:3])} to be ready"
        tail = f"; {', '.join(waiting)} not ready yet" if waiting else ""
        return ("succeeded", 100, f"{ref['claim'] or volume} is back to {ref['snapshot']}. The state before is kept "
                                  f"as snapshot {ref.get('kept', '')}, to roll forward again if needed{tail}"
                                  f"{_held(ref, ref.get('stuck', ''))}")
    return "failed", 100, f"unknown step {phase}"


def cancel_plan(item):
    ref = item["ref"]
    if ref.get("reverted"):
        return {"mode": "stop", "can": False,
                "why_not": "The volume is rolled back already; what is left is starting things again"}
    return {"mode": "rollback", "undo": ["The volume is let go and everything that was stopped is started again"],
            "keeps": ["The volume's data, which has not been changed"], "severity": "low", "needs": "admin"}


def cancel_run(item, _options):
    return _put_back(item["ref"], "Cancelled")[2]


def start_guarded(b):
    """start, holding the volume's storage fence: rolling back stops and
    starts what uses it and writes through Longhorn's own API too."""
    with STORAGE_GUARD.volume(OPS, kget, b.get("volume")):
        operation = start(str(b.get("volume") or ""), str(b.get("snapshot") or ""), OPS)
    return {"ok": True, "operation": operation,
            "detail": "Rolling back: what uses it stops first, then starts again"}


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/lh/snapshot/revert/plan"): ("viewer", lambda request: plan((request.query.get("volume") or [""])[0], (request.query.get("snapshot") or [""])[0])),
    ("POST", "/api/lh/snapshot/revert"): ("admin", lambda request: start_guarded(request.body)),
}
