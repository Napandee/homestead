"""Start a StatefulSet's stuck pod on another host (#441): the reads.

Every read here is strict. The check that decides whether a pod may be
started elsewhere is fail-closed, so a read that fails raises Unread with a
reason a person can act on, rather than being treated as "nothing there"."""
import urllib.error
import urllib.parse

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


def _q(s):
    return urllib.parse.quote(str(s), safe="")


def _ready(obj):
    for c in (obj.get("status") or {}).get("conditions") or []:
        if c.get("type") == "Ready":
            return c.get("status") == "True", c.get("lastTransitionTime")
    return False, None


def _lease(node):
    try:
        return (kget(LEASES + _q(node)).get("spec") or {}).get("renewTime")
    except Exception as e:
        if isinstance(e, urllib.error.HTTPError) and e.code == 404:
            return None
        raise Unread(f"Could not read the lease of host {node} ({e}), so Homestead cannot tell whether starting the pod elsewhere is safe.") from e


def read_state(ns, name, node):
    nodes = _read("/api/v1/nodes", "the hosts").get("items", [])
    ready = {n["metadata"]["name"]: _ready(n) for n in nodes}
    if node not in ready:
        raise Unread(f"Host {node} is not in the cluster's host list, so Homestead cannot tell its state.")
    host = {"ready": ready[node][0], "since": None if ready[node][0] else ready[node][1], "lease": _lease(node)}
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

    lh_nodes = {n["metadata"]["name"]: _ready(n)[0]
                for n in _read(f"{LH}/nodes", "the Longhorn nodes").get("items", [])}
    replicas, engines, robustness = {}, {}, {}
    for r in _read(f"{LH}/replicas", "the Longhorn replicas").get("items", []):
        s = r.get("spec") or {}
        replicas.setdefault(s.get("volumeName"), []).append(
            {"node": s.get("nodeID"), "failed": bool(s.get("failedAt")), "healthy_at": bool(s.get("healthyAt"))})
    for e in _read(f"{LH}/engines", "the Longhorn engines").get("items", []):
        s = e.get("spec") or {}
        engines.setdefault(s.get("volumeName"), []).append(
            {"node": s.get("nodeID"), "state": (e.get("status") or {}).get("currentState")})
    for v in _read("/apis/longhorn.io/v1beta2/volumes", "the Longhorn volumes").get("items", []):
        robustness[v["metadata"]["name"]] = (v.get("status") or {}).get("robustness")
    claims = {c: dict(row, longhorn=row["volume"] in robustness) for c, row in pvcs.items()}

    return {"node": host, "harvester": harvester, "set": {"ns": ns, "name": name, "wait_mode": wait},
            "pods": pods, "claims": claims, "lh_nodes": lh_nodes, "replicas": replicas, "engines": engines,
            "robustness": robustness, "nodes_ready": {n: r[0] for n, r in ready.items()}}
