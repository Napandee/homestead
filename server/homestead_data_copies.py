"""How many copies of Homestead's own data Longhorn keeps (#731 item 3).

The manifest asks for Longhorn's own class, `longhorn`, and stock Longhorn
makes that class with three copies and copies kept on different hosts. On a
cluster of one host only one copy can be placed, so Longhorn marks the
volume degraded - and every warning that reads a volume's robustness (the
dashboard, alerts, MQTT, the installer's check) says so for as long as the
cluster has one host.

fit_single_host() lowers Homestead's own data volume, and nothing else, to
the one copy that host can keep, while the cluster has exactly one
Kubernetes node and one Longhorn node record, Longhorn says a copy cannot be
placed (Scheduled False, ReplicaSchedulingFailure) and none is being rebuilt.
The copies it asked for before are kept on the volume (homestead.io/
one-host-copies). Counted are all node records: a host that is only down or
rebooting on a bigger cluster still counts as a host, so nothing is lowered
there. Any distribution, since a manifest install may be on any; never on
Harvester, whose copies are Harvester's. The count it sets is recorded as
Homestead's own (homestead_node_parity's own_copies), so on k3s and RKE2 it
rises again by itself as hosts join.

It reads through homestead_node_parity's bound kget and ksend, platform and
own Deployment, so one bind serves both.
"""
import re

import homestead_names as NAMES
import homestead_node_parity as NODE_PARITY
import homestead_shared as SHARED

GiB = 1024 ** 3
ENGINES = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/engines"
# On the volume Homestead lowered: the copies it asked for before.
LOWERED_FROM = NAMES.key("one-host-copies")
UNITS = {"": 1, "k": 1000, "M": 1000 ** 2, "G": 1000 ** 3, "T": 1000 ** 4,
         "Ki": 1024, "Mi": 1024 ** 2, "Gi": GiB, "Ti": 1024 ** 4}


def _gb(quantity):
    """A Kubernetes storage quantity ("2Gi", "500Mi") in GiB, 0.0 if unreadable."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([kMGT]i?)?", str(quantity or "").strip())
    if not match or (match.group(2) or "") not in UNITS:
        return 0.0
    return round(float(match.group(1)) * UNITS[match.group(2) or ""] / GiB, 1)


def own_data_volumes():
    """Each claim Homestead's own Deployment mounts: its claim, volume name,
    class, size and Longhorn volume (None when it is not Longhorn's). Raises
    when the Deployment or a claim cannot be read."""
    kget = NODE_PARITY.kget
    ns, name = NODE_PARITY.OWN
    if not ns or not name:
        return []
    deployment = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}") or {}
    claims = [v["persistentVolumeClaim"]["claimName"] for v in
              (((deployment.get("spec") or {}).get("template") or {}).get("spec") or {}).get("volumes") or []
              if (v.get("persistentVolumeClaim") or {}).get("claimName")]
    found = []
    for claim in claims:
        spec = (kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{claim}") or {}).get("spec") or {}
        pv = spec.get("volumeName") or ""
        try:
            volume = kget(f"{NODE_PARITY.LH_VOLUMES}/{pv}") if pv else None
        except Exception as error:
            if getattr(error, "code", None) != 404:
                raise
            volume = None
        size = int(((volume or {}).get("spec") or {}).get("size") or 0)
        found.append({"claim": claim, "pv": pv, "storage_class": spec.get("storageClassName") or "",
                      "size_gb": round(size / GiB, 1) if size else
                      _gb(((spec.get("resources") or {}).get("requests") or {}).get("storage")),
                      "volume": volume if isinstance(volume, dict) else None})
    return found


def _one_host():
    """True only when the cluster has exactly one Kubernetes node and exactly
    one Longhorn node record, whatever their state."""
    kget = NODE_PARITY.kget
    nodes = (kget("/api/v1/nodes") or {}).get("items")
    lh_nodes = (kget(NODE_PARITY.LH_NODES) or {}).get("items")
    return isinstance(nodes, list) and isinstance(lh_nodes, list) and len(nodes) == 1 and len(lh_nodes) == 1


def _cannot_place(volume):
    """Longhorn's own proof that a copy cannot be placed: the volume's
    Scheduled condition False for ReplicaSchedulingFailure. Degraded alone is
    not proof - copies rebuilding after a reboot read degraded too."""
    conditions = (volume.get("status") or {}).get("conditions") or []
    if isinstance(conditions, dict):
        conditions = list(conditions.values())
    return any(isinstance(c, dict) and str(c.get("type", "")).lower() == "scheduled"
               and c.get("status") == "False" and c.get("reason") == "ReplicaSchedulingFailure"
               for c in conditions)


def _rebuilding(pv):
    """Whether any copy of the volume is being rebuilt (WO). Raises when the
    engines cannot be read."""
    found = NODE_PARITY.kget(f"{ENGINES}?labelSelector=longhornvolume%3D{pv}") or {}
    return any(mode == "WO" for engine in found.get("items") or []
               if ((engine.get("spec") or {}).get("volumeName") or pv) == pv
               for mode in ((engine.get("status") or {}).get("replicaModeMap") or {}).values())


def fit_single_host():
    """On a one-host Longhorn cluster, Homestead's own data volume asks for
    the one copy that host can keep. Returns a line for the log, or None.
    Read failures change nothing; a refused PATCH raises to the caller."""
    p = NODE_PARITY.platform() or {}
    if p.get("harvester") or not p.get("longhorn"):
        return None
    try:
        if not _one_host():
            return None
        volumes = own_data_volumes()
    except Exception:
        return None
    lowered = []
    for found in volumes:
        volume = found["volume"]
        if not volume:
            continue
        copies = int((volume.get("spec") or {}).get("numberOfReplicas") or 0)
        # Only when Longhorn says a copy cannot be placed, and none is being
        # rebuilt: copies the host can hold (soft anti-affinity, several
        # disks) are never thrown away.
        if copies <= 1 or (volume.get("status") or {}).get("robustness") != "degraded" or not _cannot_place(volume):
            continue
        try:
            if _rebuilding(found["pv"]):
                continue
        except Exception:
            continue
        NODE_PARITY.ksend("PATCH", f"{NODE_PARITY.LH_VOLUMES}/{found['pv']}",
                          {"metadata": {"annotations": {LOWERED_FROM: str(copies)}}, "spec": {"numberOfReplicas": 1}},
                          ctype="application/merge-patch+json")
        lowered.append(found["claim"])
    if not lowered:
        return None
    state = NODE_PARITY._load()
    state["own_copies"] = 1
    SHARED.write_json(NODE_PARITY._path(), state, indent=1, sort_keys=True)
    return "; ".join(f"Homestead's data volume {claim} keeps 1 copy: this cluster has one host" for claim in lowered)
