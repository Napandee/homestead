"""Whether an address Homestead sees is a client's, or a node's (#420).

Homestead's Service is a LoadBalancer with externalTrafficPolicy Cluster:
the node a request reaches rewrites its source to the node's own address on
the pod network (k3s: 10.42.x.0) before handing it on. Homestead then sees
that node, not the client - so a key held to the client's network is
refused, every client through one node shares its sign-in lockout, and the
histories show nodes. Preserving client addresses is planned (#422); until
then Homestead says so wherever an address matters, rather than leaving a
refusal like "cannot be used from 10.42.0.0" unexplained.

A node address is one inside a node's pod network (spec.podCIDRs), or one
of a node's own addresses. Both are read from the nodes, at most every TTL.
"""
import ipaddress
import threading
import time

TTL = 300
kget = None
_known = {"at": 0.0, "networks": [], "hosts": set()}
_lock = threading.Lock()


def bind(_kget):
    global kget
    kget = _kget


def _refresh(now):
    networks, hosts = [], set()
    for node in kget("/api/v1/nodes").get("items", []):
        spec, status = node.get("spec") or {}, node.get("status") or {}
        for cidr in spec.get("podCIDRs") or ([spec["podCIDR"]] if spec.get("podCIDR") else []):
            try:
                networks.append(ipaddress.ip_network(cidr, strict=False))
            except ValueError:
                continue
        for address in status.get("addresses") or []:
            if address.get("type") in ("InternalIP", "ExternalIP"):
                hosts.add(str(address.get("address") or ""))
    _known.update(at=now, networks=networks, hosts=hosts)


def is_node_address(addr, now=None):
    """Whether addr is a node's rather than a client's; False when it cannot
    be told (no cluster to ask, or not an address)."""
    try:
        ip = ipaddress.ip_address(str(addr or "").strip())
    except ValueError:
        return False
    now = now or time.time()
    with _lock:
        if kget and now - _known["at"] > TTL:
            try:
                _refresh(now)
            except Exception:
                _known["at"] = now          # asked again after TTL, not on every request
        networks, hosts = list(_known["networks"]), set(_known["hosts"])
    return str(ip) in hosts or any(ip in net for net in networks if net.version == ip.version)


NOTE = ("{addr} is an address inside the cluster - a node's - not the client's: requests reach Homestead through "
        "its Service, which rewrites where they come from. Homestead cannot tell clients apart here, so a key "
        "held to networks is refused; make keys without a network limit until client addresses are preserved.")


def note(addr):
    """What to say about addr, or "" when it is a client's own."""
    return NOTE.format(addr=addr) if is_node_address(addr) else ""


def about(addr):
    """For the app: the address a request came from, and whether it is a node's."""
    said = note(addr)
    return {"address": str(addr or ""), "node_address": bool(said), "note": said}
