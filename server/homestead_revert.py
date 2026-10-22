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
everything started again, unchanged. So does a failure of Longhorn's API at
any step up to the revert: the job ends, it does not retry with the app
stopped. Before anything is stopped, Longhorn's API must answer.
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
WAIT = {"stopping": 600, "attaching": 300, "detaching": 300, "starting": 600}

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
        raise ValueError(f"Longhorn refused it: {detail or error.reason}") from error
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


def _put_back(ref, why, stuck=""):
    """Before or at the revert: let go of the volume, start everything again,
    and say why - and what may have changed."""
    try:
        if ref.get("attached"):
            stuck = _let_go(ref)
    finally:
        if ref.get("namespace"):
            reclass._start(ref["namespace"], ref)
    if ref.get("reverting"):
        changed = (f"Longhorn may have rolled it back before failing: the state before is kept as snapshot "
                   f"{ref.get('kept', '')}, to roll back to if needed")
    elif ref.get("kept"):
        changed = f"Nothing was rolled back; the state it is in is also kept as snapshot {ref['kept']}"
    else:
        changed = "Nothing was changed"
    return "failed", 100, f"{why}. {changed}; everything that was running is starting again{_held(ref, stuck)}"


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
        try:
            lh_call("POST", f"/volumes/{volume}?action=attach",
                    {"hostId": node, "disableFrontend": True, "attachedBy": "", "attacherType": "longhorn-api",
                     "attachmentID": ATTACHMENT})
        except Exception as error:
            # Longhorn may have taken the ticket before failing: drop it
            # through Kubernetes, which needs no answer from Longhorn's API.
            return _put_back(ref, f"Longhorn's API did not attach {volume} for the rollback: {_why(error)}",
                             _drop_ticket(ref))
        ref.update(node=node, attached=True)
        _phase(ref, "attaching")
        return "running", 30, f"Attaching {volume} to {node} for the rollback"
    if phase == "attaching":
        if _state(volume) != "attached":
            if _late(ref):
                return _put_back(ref, f"{volume} did not attach for the rollback")
            return "running", 35, f"Attaching {volume} to {ref.get('node')}"
        keep = f"before-rollback-{int(time.time())}"
        try:
            lh_call("POST", f"/volumes/{volume}?action=snapshotCreate", {"name": keep})
            ref["kept"] = keep
            ref["reverting"] = True
            lh_call("POST", f"/volumes/{volume}?action=snapshotRevert", {"name": ref["snapshot"]})
        except Exception as error:
            # Not retried with the app stopped: put everything back now.
            return _put_back(ref, f"Longhorn's API failed during the rollback: {_why(error)}")
        ref["reverted"] = True
        _phase(ref, "detaching")
        _let_go(ref)            # retried while detaching, if it failed
        return "running", 70, f"Rolled back to {ref['snapshot']}; the state before is kept as {keep}"
    if phase == "detaching":
        if ref.get("attached"):
            stuck = _let_go(ref)
            if stuck and not _late(ref):
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
