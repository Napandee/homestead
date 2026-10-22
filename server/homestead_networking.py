"""Service, VIP, port, endpoint and ingress inventory for Homestead.

Harvester's management cluster advertises LoadBalancer Services with kube-vip.
There is no kube-vip cloud-controller allocating addresses on this installation,
so Homestead always resolves automatic requests to a concrete address before it
creates a Service.  Allocation is deliberately reconciled against live cluster
state rather than trusting an IPPool's allocation counter: explicitly requested
kube-vip addresses are not recorded in the Harvester IPPool status.

Split by responsibility (#623): the clients bind() sets and the shared readers
live in homestead_network_state, the network picture and address checks in
homestead_network_inventory, the Services that publish an app in
homestead_network_services, the addresses Homestead may hand out in
homestead_vip_registry, and the VM networks in homestead_network_vms. This
module keeps bind(), the guard and node probes server.py hands it, and the
routes, and re-exports every moved name except the bound clients for one
release. Replace a name in the module that defines it: the bound clients
(kget, ksend, SYSTEM_NAMESPACES, DEFAULT_NAMESPACE, SHARED_VIP) are only on
homestead_network_state.
"""
import homestead_fleet as FLEET
import homestead_ipam as IPAM
import homestead_operations as OPS
import homestead_self_address as SELF_ADDRESS
import homestead_routes
import homestead_network_state as STATE
import homestead_network_inventory as INV
import homestead_network_services as SVC
import homestead_vip_registry as REG
import homestead_network_vms as VMNET

# server.py's: the refusal to touch the SMB server Homestead runs, and what
# the node probes report (a VM network's host interfaces).
guard_managed = lambda namespace, name: None
node_probes = lambda: {}


def bind(_kget, _ksend, system_namespaces, default_namespace, shared_vip, _guard_managed=None, _node_probes=None):
    global guard_managed, node_probes
    STATE.kget, STATE.ksend = _kget, _ksend
    STATE.SYSTEM_NAMESPACES = set(system_namespaces or ())
    STATE.DEFAULT_NAMESPACE = default_namespace
    STATE.SHARED_VIP = shared_vip
    guard_managed = _guard_managed or guard_managed
    node_probes = _node_probes or node_probes


def _create_service_route(request):
    b = request.body
    guard_managed(b.get("namespace") or STATE.DEFAULT_NAMESPACE, b.get("name"))
    result = SVC.create_service(b)
    homestead_routes.forget("network", "flow2")
    result["operation"] = OPS.start(
        "network-service", f"Expose {result['name']}",
        {"kind": "Service", "name": result["name"], "namespace": result["namespace"]},
        "/networking", {"namespace": result["namespace"], "name": result["name"]},
        "Waiting for the Service address and endpoints")
    return result


def _delete_service_route(request):
    b = request.body
    guard_managed(b.get("namespace"), b.get("name"))
    result = SVC.delete_service(b.get("namespace"), b.get("name"), b.get("force"))
    homestead_routes.forget("network")
    return result


def _default_vip_route(request):
    result = REG.set_default_vip(request.body.get("ip", ""))
    homestead_routes.forget("network", "ov")
    return result


def _change_vip_route(request):
    b = request.body
    result = REG.change_vip(b.get("old", ""), b.get("new", ""), apply=b.get("apply") is True)
    if b.get("apply") is True:
        homestead_routes.forget("network", "ov")
        # Backups follow the store to its new address. Imported here: the
        # object store imports this module.
        import homestead_objectstore as OBJECTS
        if any(s["name"].startswith(OBJECTS.NAME) and s["namespace"] == OBJECTS.NS for s in result["services"]):
            try:
                pointed = OBJECTS.request_target()
                result["detail"] += "; " + pointed["detail"]
            except Exception as error:
                result["detail"] += f"; Longhorn's backup target was not changed: {str(error)[:120]}"
        # Linked clusters reach this one at its new VIP, where it was on the old.
        try:
            if FLEET.follow_address((f"http://{str(b.get('old') or '').strip()}:{SELF_ADDRESS.WEB_PORT}",)):
                result["detail"] += "; linked clusters were told the new address"
        except Exception:
            pass
    return result


def _fresh(call):
    """The network picture dropped from server.py's cache, then call()."""
    homestead_routes.forget("network")
    return call()


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("POST", "/api/network/plan"): ("operator", lambda request: SVC.service_plan(request.body)),
    ("GET", "/api/network"): ("viewer", lambda request: homestead_routes.cached("network", 5, INV.inventory)),
    ("POST", "/api/network/services"): ("operator", _create_service_route),
    ("POST", "/api/network/service/delete"): ("admin", _delete_service_route),
    ("POST", "/api/network/vips/add"): ("admin", lambda request: _fresh(
        lambda: REG.add_vips(request.body, IPAM.load()[0].get("records") or {}))),
    ("POST", "/api/network/vips/default"): ("admin", _default_vip_route),
    ("POST", "/api/network/vips/remove"): ("admin", lambda request: _fresh(lambda: REG.remove_vip(request.body.get("ip", "")))),
    ("POST", "/api/network/vips/change"): ("admin", _change_vip_route),
    ("POST", "/api/network/vips/label"): ("admin", lambda request: _fresh(
        lambda: REG.set_vip_label(request.body.get("ip", ""), request.body.get("label", "")))),
    ("POST", "/api/network/vm-networks"): ("admin", lambda request: _fresh(
        lambda: VMNET.create_vm_network(dict(request.body, _probes=node_probes())))),
}


# Every moved name, for one release (#623).
from homestead_network_state import (  # noqa: E402,F401
    DNS_NAME, VIP_ANNOTATION, _items, _unread, _complete, _read, _name, _ipv4,
)
from homestead_network_inventory import (  # noqa: E402,F401
    _endpoint_index, _access_url, _pool_inventory, _ingress_inventory, HARVESTER_VIP, _ours,
    SHARE_KEY, _shares, _address_owners, address_problem, check_address, cluster_address_problem,
    inventory, _controller, node_addresses_only, servicelb_present,
)
from homestead_network_services import (  # noqa: E402,F401
    _node_port_owner, _node_port_problem, _ports, service_plan, create_service, prepare_deploy,
    workload_services, _listener_owner, sync_workload_ports, _own_service, set_workload_address,
    delete_service, workload_service_names,
)
from homestead_vip_registry import (  # noqa: E402,F401
    VIP_MAP, MAX_ADD, _NothingAdded, _vip_map_path, registered, shared_vip, set_default_vip,
    _update_registered, add_vips, remove_vip, VIP_KEYS, change_vip, set_vip_label,
)
from homestead_network_vms import (  # noqa: E402,F401
    CLUSTER_NETWORKS, NAD_API, MULTUS_HELP, _macvtap, _multus, host_interfaces, vm_network_options,
    _vlan, _host_network_config, create_vm_network,
)
