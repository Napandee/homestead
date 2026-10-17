"""Start a StatefulSet's stuck pod on another host (#441): the reads, the plan, the routes and the job.

Every read here is strict. The check that decides whether a pod may be
started elsewhere is fail-closed, so a read that fails raises Unread with a
reason a person can act on, rather than being treated as "nothing there"."""
import hashlib
import json
import time
import urllib.error
import urllib.parse
from datetime import datetime, timezone

import homestead_failover as FAILOVER
import homestead_hostrun as HOSTRUN
import homestead_operations as OPS
import homestead_routes as ROUTER

LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
LEASES = "/apis/coordination.k8s.io/v1/namespaces/kube-node-lease/leases/"

# Wired like homestead_promote: server.py is at its line cap.
kget = lambda path, *a, **k: HOSTRUN.kget(path, *a, **k)  # noqa: E731
ksend = lambda *a, **k: HOSTRUN.ksend(*a, **k)  # noqa: E731


clock = time.time  # tests replace it


def bind(_kget, _ksend):
    global kget, ksend
    kget, ksend = _kget, _ksend


class Unread(Exception):
    """Something the check needs could not be read; the message says what."""


def _read(path, what):
    try:
        return kget(path)
    except Exception as e:
        raise Unread(f"Could not read {what} ({e}), so Homestead cannot tell whether starting the pod elsewhere is safe.") from e


def _need(v):
    if not v:
        raise KeyError("empty field")
    return v


def _q(s):
    return urllib.parse.quote(str(s), safe="")


def _ready(obj):
    for c in (obj.get("status") or {}).get("conditions") or []:
        if c.get("type") == "Ready":
            return c.get("status") == "True", c.get("lastTransitionTime")
    return False, None


def _cond(obj):
    """The Ready condition's (status string, lastTransitionTime); (None, None) if absent."""
    for c in (obj.get("status") or {}).get("conditions") or []:
        if c.get("type") == "Ready":
            return c.get("status"), c.get("lastTransitionTime")
    return None, None


def _lease(node):
    try:
        return (kget(LEASES + _q(node)).get("spec") or {}).get("renewTime")
    except Exception as e:
        if isinstance(e, urllib.error.HTTPError) and e.code == 404:
            return None
        raise Unread(f"Could not read the lease of host {node} ({e}), so Homestead cannot tell whether starting the pod elsewhere is safe.") from e


def read_state(ns, name, node):
    try:
        return _read_state(ns, name, node)
    except Unread:
        raise
    except (KeyError, TypeError, AttributeError, IndexError, ValueError) as e:
        raise Unread(f"Kubernetes or Longhorn answered in a form Homestead can't read: {type(e).__name__} {e}") from e


def _read_state(ns, name, node):
    nodes = _read("/api/v1/nodes", "the hosts").get("items", [])
    ready = {n["metadata"]["name"]: _ready(n) for n in nodes}
    if node not in ready:
        raise Unread(f"Host {node} is not in the cluster's host list, so Homestead cannot tell its state.")
    status = next((_cond(n)[0] for n in nodes if n["metadata"]["name"] == node), None)
    host = {"ready": ready[node][0], "status": status,
            "since": None if ready[node][0] else ready[node][1], "lease": _lease(node)}
    groups = _read("/apis", "the API groups").get("groups", [])
    harvester = "harvesterhci.io" in [g.get("name") for g in groups]

    sts = _read(f"/apis/apps/v1/namespaces/{_q(ns)}/statefulsets/{_q(name)}", f"StatefulSet {ns}/{name}")
    spec = sts.get("spec") or {}
    wait = FAILOVER.mode_of((spec.get("template") or {}).get("spec")) == "wait"
    labels = (spec.get("selector") or {}).get("matchLabels") or {}
    if not labels:
        raise Unread(f"StatefulSet {ns}/{name} has no label selector, so Homestead cannot find its pods.")
    selector = urllib.parse.quote(",".join(f"{k}={v}" for k, v in sorted(labels.items())))
    items = _read(f"/api/v1/namespaces/{_q(ns)}/pods?labelSelector={selector}", f"the pods of {ns}/{name}").get("items", [])
    pods, claim_names = [], []
    for p in items:
        meta = p.get("metadata") or {}
        owned = any(o.get("kind") == "StatefulSet" and o.get("name") == name for o in meta.get("ownerReferences") or [])
        if not owned or (p.get("spec") or {}).get("nodeName") != node:
            continue
        claims = [v["persistentVolumeClaim"]["claimName"] for v in (p["spec"].get("volumes") or [])
                  if v.get("persistentVolumeClaim")]
        claim_names += [c for c in claims if c not in claim_names]
        pods.append({"name": meta.get("name"), "uid": meta.get("uid"), "node": node,
                     "terminating": bool(meta.get("deletionTimestamp")), "claims": claims})

    pvcs = {}
    for c in claim_names:
        pvc = _read(f"/api/v1/namespaces/{_q(ns)}/persistentvolumeclaims/{_q(c)}", f"claim {c}")
        s = pvc.get("spec") or {}
        pvcs[c] = {"access": list(s.get("accessModes") or []), "volume": s.get("volumeName") or None}

    lh_nodes = {}
    for n in _read(f"{LH}/nodes", "the Longhorn nodes").get("items", []):
        st = _cond(n)[0]
        lh_nodes[n["metadata"]["name"]] = None if st is None else st == "True"
    replicas, engines, robustness = {}, {}, {}
    for r in _read(f"{LH}/replicas", "the Longhorn replicas").get("items", []):
        s = r["spec"]  # a missing field raises, which read_state turns into Unread
        vol = _need(s["volumeName"])
        if not s.get("nodeID"):
            continue  # a replacement Longhorn has not placed yet: never a copy, never on the host
        replicas.setdefault(vol, []).append(
            {"node": s["nodeID"], "failed": bool(s.get("failedAt")), "healthy_at": bool(s.get("healthyAt"))})
    for e in _read(f"{LH}/engines", "the Longhorn engines").get("items", []):
        s = e["spec"]
        engines.setdefault(_need(s["volumeName"]), []).append(
            {"node": _need(s["nodeID"]), "state": (e.get("status") or {}).get("currentState")})
    for v in _read("/apis/longhorn.io/v1beta2/volumes", "the Longhorn volumes").get("items", []):
        robustness[v["metadata"]["name"]] = (v.get("status") or {}).get("robustness")
    claims = {c: dict(row, longhorn=row["volume"] in robustness) for c, row in pvcs.items()}

    return {"node": host, "harvester": harvester, "set": {"ns": ns, "name": name, "wait_mode": wait},
            "pods": pods, "claims": claims, "lh_nodes": lh_nodes, "replicas": replicas, "engines": engines,
            "robustness": robustness, "nodes_ready": {n: r[0] for n, r in ready.items()}}


MIN_DOWN = 600  # seconds the host must have been NotReady


def _epoch(s):
    """Seconds since the epoch, or None when `s` is not a timestamp."""
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except (AttributeError, ValueError):
        return None


def _copies(state, volume, node):
    """Healthy copies of `volume` on hosts other than `node`, by host name: [(host, is_ready)]."""
    return sorted((r["node"], bool(state["nodes_ready"].get(r["node"])))
                  for r in state["replicas"].get(volume) or []
                  if r["node"] != node and not r["failed"] and r["healthy_at"])


def _chosen_copy(state, volume, node):
    """First healthy copy, by host name, on a Ready host other than `node`."""
    return next((n for n, up in _copies(state, volume, node) if up), None)


def plan(state, now):
    """What would be done, and what stands in the way. Pure: no reads.

    The host's name is taken from its pods (read_state keeps only pods on the
    host); with no pod there is nothing to start, which is itself a blocker."""
    st, pods = state["node"], state["pods"]
    node = pods[0]["node"] if pods else None
    blockers, volumes = [], []
    down_seconds = None
    if state["harvester"]:
        blockers.append("This cluster runs Harvester, which manages its own hosts and volumes; starting a pod elsewhere is not supported here.")
    if st["ready"]:
        blockers.append("The host is Ready, so its pod is not stuck on a down host.")
    elif st.get("status") != "Unknown":
        blockers.append(f"The host's kubelet is still answering (Ready status: {st.get('status')}), so it is not unreachable.")
    since = _epoch(st["since"]) if st["since"] else None
    if not st["ready"]:
        if since is None:
            blockers.append("Homestead can't read when the host went down.")
        else:
            down_seconds = int(now - since)
            if down_seconds < MIN_DOWN:
                blockers.append(f"The host has been down for {down_seconds // 60} minutes; it must be down for at least 10 minutes first.")
        heard = _epoch(st["lease"]) if st["lease"] else None
        if heard is None:
            blockers.append("Homestead can't tell when the host was last heard from.")
        elif now - heard < MIN_DOWN:
            blockers.append("The host was heard from less than 10 minutes ago.")
    if not pods:
        blockers.append("There is no pod of this StatefulSet on the host.")
    else:
        lh = state["lh_nodes"].get(node)
        if lh is None:
            blockers.append(f"Longhorn does not report {node} as down, so Homestead cannot tell whether its volumes are still attached.")
        elif lh:
            blockers.append(f"Longhorn still sees {node} as Ready, so its volumes may still be in use.")
    seen = set()
    for pod in pods:
        for pvc in pod["claims"]:
            if pvc in seen:
                continue
            seen.add(pvc)
            c = state["claims"].get(pvc)
            if not c or not c["volume"] or not c["longhorn"]:
                blockers.append(f"{pvc}: it is not a bound Longhorn volume, so Homestead cannot check where its data is.")
                continue
            vol = c["volume"]
            if "ReadWriteMany" in c["access"]:
                blockers.append(f"{pvc}: it is ReadWriteMany (shared), which this action does not handle.")
            for e in state["engines"].get(vol) or []:
                if e["node"] == node and e["state"] != "stopped":
                    blockers.append(f"{pvc}: Longhorn can't confirm the engine of {vol} on {node} has stopped (state: {e['state']}).")
                    break
            rob = state["robustness"].get(vol)
            if rob not in ("healthy", "degraded"):
                blockers.append(f"{pvc}: Longhorn reports {vol} as {rob}, not healthy or degraded.")
            copy_on = _chosen_copy(state, vol, node)
            if copy_on is None:
                others = [n for n, _ in _copies(state, vol, node)]
                if others:
                    blockers.append(f"{pvc}: its only other copy is on {others[0]}, which is not Ready either.")
                else:
                    blockers.append(f"{pvc}: its volume {vol} has no healthy copy on another host.")
            volumes.append({"pvc": pvc, "volume": vol, "copy_on": copy_on})
    review = {"node": node, "set": [state["set"]["ns"], state["set"]["name"]],
              "pods": sorted(p["uid"] for p in pods),
              "copies": sorted([v["volume"], v["copy_on"]] for v in volumes)}
    token = hashlib.sha256(json.dumps(review, sort_keys=True).encode()).hexdigest()[:20]
    return {"ready": not blockers, "blockers": blockers, "node": node, "down_since": st["since"],
            "down_seconds": down_seconds, "last_heard": st["lease"], "set": dict(state["set"]),
            "pods": [{"name": p["name"], "uid": p["uid"], "terminating": p["terminating"]} for p in pods],
            "volumes": volumes, "review_token": token}


KIND = "set-restart"
LIMIT = 900  # seconds the job follows the replacement pods
STAGES = ("deleted", "scheduled", "attaching volume", "ready")


def _plan(request):
    q = request.query
    arg = lambda k: (q.get(k) or [""])[0]  # noqa: E731
    try:
        return plan(read_state(arg("ns"), arg("name"), arg("node")), clock())
    except Unread as e:
        return {"ready": False, "blockers": [str(e)]}


def _active(ns, name):
    # The stored records: the public list leaves each job's ref out.
    for item in OPS._read():
        ref = item.get("ref") or {}
        if item.get("kind") == KIND and (ref.get("ns"), ref.get("name")) == (ns, name) and item.get("status") not in OPS.TERMINAL:
            return item["id"]
    return ""


def _run(request):
    """Refused (409) whenever the review it carries no longer holds, so the
    browser shows the plan again. Order as node power: confirm, running job,
    re-plan, blockers, token, then act."""
    b = request.body
    ns, name, node = str(b.get("ns") or ""), str(b.get("name") or ""), str(b.get("node") or "")
    if not name or b.get("confirm") != name:
        raise ValueError("confirmation must repeat the StatefulSet name")
    running = _active(ns, name)
    if running:
        return ROUTER.Reply(409, {"error": "A restart for this StatefulSet is still running; follow it instead of sending another",
                                  "operation": running})
    try:
        now = clock()
        reviewed = plan(read_state(ns, name, node), now)
    except Unread as e:
        return ROUTER.Reply(409, {"error": str(e), "plan": {"ready": False, "blockers": [str(e)]}})
    if not reviewed["ready"]:
        return ROUTER.Reply(409, {"error": "; ".join(reviewed["blockers"]), "plan": reviewed})
    if b.get("review_token") != reviewed["review_token"]:
        return ROUTER.Reply(409, {"error": "what the host holds changed; review the plan again", "plan": reviewed})
    deleted, replaced, failed = [], [], []
    for pod in reviewed["pods"]:
        try:
            ksend("DELETE", f"/api/v1/namespaces/{_q(ns)}/pods/{_q(pod['name'])}",
                  {"apiVersion": "v1", "kind": "DeleteOptions", "gracePeriodSeconds": 0, "preconditions": {"uid": pod["uid"]}})
            deleted.append(pod)
        except urllib.error.HTTPError as e:
            if e.code in (404, 409):
                replaced.append(pod)  # gone or already replaced: never retried
            else:
                failed.append(f"{pod['name']} ({e})")
        except Exception as e:
            failed.append(f"{pod['name']} ({e})")
    if failed and not deleted and not replaced:
        raise ValueError("Kubernetes did not delete " + ", ".join(failed))
    stage = "deleted" if deleted else "already replaced"
    ref = {"ns": ns, "name": name, "node": reviewed["node"], "started": now, "started_by": request.user,
           "stage": stage, "pods": [{"name": p["name"], "uid": p["uid"]} for p in reviewed["pods"] if p not in failed]}
    message = "Deleted " + ", ".join(p["name"] for p in deleted) + "; waiting for a replacement on another host"
    if replaced:
        message += ("; " if deleted else "") + ", ".join(p["name"] for p in replaced) + " already replaced"
    if failed:
        message += "; could not delete " + ", ".join(failed)
    op = OPS.start(KIND, f"Start {name} on another host", {"kind": "StatefulSet", "name": name, "namespace": ns},
                   "/nodes", ref, message=message)
    return ROUTER.Reply(202, op)


def _pod_stage(pod, uid, node):
    """How far one replacement has got: (rank into STAGES, text)."""
    meta, spec = pod.get("metadata") or {}, pod.get("spec") or {}
    where = spec.get("nodeName")
    if meta.get("uid") == uid or not where or where == node:
        return 0, STAGES[0]
    if _ready(pod)[0]:
        return 3, "ready"
    status = pod.get("status") or {}
    started = status.get("containerStatuses") or any(
        c.get("type") == "Initialized" and c.get("status") == "True" for c in status.get("conditions") or [])
    return (2, "attaching volume") if started else (1, f"scheduled on {where}")


def status(item):
    """(status, progress, message): succeeded when every reviewed pod has a
    new Ready pod on another host; failed after LIMIT seconds."""
    ref = item["ref"]
    waited = clock() - float(ref.get("started") or 0)
    rows, unread = [], None
    for p in ref.get("pods") or []:
        try:
            pod = kget(f"/api/v1/namespaces/{_q(ref['ns'])}/pods/{_q(p['name'])}")
            rows.append((p["name"],) + _pod_stage(pod, p["uid"], ref["node"]))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                rows.append((p["name"], 0, STAGES[0]))  # not recreated yet
            else:
                unread = e
        except Exception as e:
            unread = e
    if rows and not unread:
        low = min(rows, key=lambda r: r[1])
        ref["stage"] = low[2]
        if low[1] == 3:
            return "succeeded", 100, "Ready on another host: " + ", ".join(r[0] for r in rows)
    if waited > LIMIT:
        return "failed", 100, (f"No replacement was Ready on another host within 15 minutes; "
                               f"the last stage reached was {ref.get('stage') or STAGES[0]}")
    if unread:
        return "running", item.get("progress") or 10, f"Waiting to read the pods again ({unread}); last stage: {ref.get('stage')}"
    rank = min(r[1] for r in rows) if rows else 0
    names = ", ".join(r[0] for r in rows if r[1] == rank)
    return "running", (10, 40, 70)[rank], f"{names}: {ref['stage']}"


# The job's own, registered at import like homestead_promote's. Nothing to
# cancel: the delete is done when the job starts.
OPS.RESOLVERS[KIND] = status

ROUTES = {
    ("GET", "/api/statefulset/restart-elsewhere/plan"): ("viewer", _plan),
    ("POST", "/api/statefulset/restart-elsewhere"): ("admin", _run),
}
