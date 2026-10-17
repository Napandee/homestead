"""Start a StatefulSet's stuck pod on another host (#441): the reads.

Every read here is strict. The check that decides whether a pod may be
started elsewhere is fail-closed, so a read that fails raises Unread with a
reason a person can act on, rather than being treated as "nothing there"."""
import hashlib
import json
import urllib.error
import urllib.parse
from datetime import datetime, timezone

import homestead_failover as FAILOVER
import homestead_hostrun as HOSTRUN

LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
LEASES = "/apis/coordination.k8s.io/v1/namespaces/kube-node-lease/leases/"

# Wired like homestead_promote: server.py is at its line cap.
kget = lambda path, *a, **k: HOSTRUN.kget(path, *a, **k)  # noqa: E731
ksend = lambda *a, **k: HOSTRUN.ksend(*a, **k)  # noqa: E731


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
