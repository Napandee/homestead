"""Each module registers its own kinds of job with register(ops), and
server.py only calls it (#622 step 3)."""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_host_bonds as HOST_BONDS
import homestead_host_bridge as HOST_BRIDGE
import homestead_host_console as HOST_CONSOLE
import homestead_host_os as HOST_OS
import homestead_k3scluster as K3SC
import homestead_os_rollout as OS_ROLLOUT
import homestead_power as POWER
import homestead_uplinks as UPLINKS
import homestead_components as COMPONENTS
import homestead_container_rebalance as CREBALANCE
import homestead_lhv2_setup as LHV2_SETUP
import homestead_longhorn as LH
import homestead_move_engine as MOVE_ENGINE
import homestead_rebalance as REBALANCE
import homestead_shares as SHARES
import homestead_unraid_vms as UNRAID_VMS
import homestead_vm_mutation_job as VM_MUTATION_JOB

# module, the kinds it resolves, the kinds it cancels
HOSTS = [
    (HOST_OS, {"host-os": HOST_OS.status}, {"host-os"}),
    (OS_ROLLOUT, {"os-rollout": OS_ROLLOUT.status}, {}),
    (HOST_BRIDGE, {"host-bridge": HOST_BRIDGE.status}, {"host-bridge"}),
    (HOST_BONDS, {HOST_BONDS.KIND: HOST_BONDS.status}, {HOST_BONDS.KIND}),
    (UPLINKS, {UPLINKS.KIND: UPLINKS.status}, {}),
    (HOST_CONSOLE, {"host-console": HOST_CONSOLE.status}, {}),
    (K3SC, {"k3s-cluster": K3SC.status}, {}),
    (POWER, {"node-power": POWER.status}, {"node-power"}),
]
# Storage and app jobs (step 3, part 2)
STORAGE_APPS = [
    (REBALANCE, {REBALANCE.KIND: REBALANCE.status}, {REBALANCE.KIND}),
    (CREBALANCE, {CREBALANCE.KIND: CREBALANCE.status}, {CREBALANCE.KIND}),
    (COMPONENTS, {"platform-upgrade": COMPONENTS.status}, {"platform-upgrade"}),
    (VM_MUTATION_JOB, {"vm-create": VM_MUTATION_JOB.status, "vm-edit": VM_MUTATION_JOB.status}, {"vm-create", "vm-edit"}),
    (LH, {"protect-run": LH.run_status}, {}),
    (MOVE_ENGINE, {"move": MOVE_ENGINE.op_state}, {}),
    (LHV2_SETUP, {"longhorn-v2-prepare": LHV2_SETUP.progress}, {}),
    (SHARES, {"share-remove": SHARES.removal_progress}, {}),
    (UNRAID_VMS, {UNRAID_VMS.KIND: UNRAID_VMS.status}, {UNRAID_VMS.KIND}),
]
ALIASES = {"homestead_host_os": "HOST_OS", "homestead_os_rollout": "OS_ROLLOUT", "homestead_host_bridge": "HOST_BRIDGE",
           "homestead_host_bonds": "HOST_BONDS", "homestead_uplinks": "UPLINKS", "homestead_host_console": "HOST_CONSOLE",
           "homestead_k3scluster": "K3SC", "homestead_power": "POWER", "homestead_rebalance": "REBALANCE",
           "homestead_container_rebalance": "CREBALANCE", "homestead_components": "COMPONENTS",
           "homestead_vm_mutation_job": "VM_MUTATION_JOB", "homestead_longhorn": "LH", "homestead_move_engine": "MOVE_ENGINE",
           "homestead_lhv2_setup": "LHV2_SETUP", "homestead_shares": "SHARES", "homestead_unraid_vms": "UNRAID_VMS"}


def fake_ops():
    return SimpleNamespace(RESOLVERS={}, CANCELLERS={}, RESUMABLE={}, LOGGERS={}, CLEANUPS=set(), CDI_CANCELLABLE=set())


class HostKindTests(unittest.TestCase):
    def test_each_host_module_registers_its_kinds(self):
        for module, resolves, cancels in HOSTS + STORAGE_APPS:
            with self.subTest(module=module.__name__):
                ops = fake_ops()
                module.register(ops)
                self.assertEqual(resolves, ops.RESOLVERS)
                self.assertEqual(set(cancels), set(ops.CANCELLERS))

    def test_a_node_power_command_cannot_be_cancelled_once_sent(self):
        ops = fake_ops()
        POWER.register(ops)
        plan, run = ops.CANCELLERS["node-power"]
        self.assertEqual({"can": False, "why_not": "A host power command cannot be cancelled after it has been sent"}, plan({}))
        self.assertEqual("", run({}, {}))

    def test_the_server_has_them_registered(self):
        import server
        for module, resolves, cancels in HOSTS + STORAGE_APPS:
            for kind, resolve in resolves.items():
                self.assertIs(resolve, server.OPS.RESOLVERS[kind], kind)
            for kind in cancels:
                ops = fake_ops()
                module.register(ops)
                # The same functions: by name, since a lambda is made afresh each time.
                same = lambda pair: [(f.__module__, f.__qualname__) for f in pair]
                self.assertEqual(same(ops.CANCELLERS[kind]), same(server.OPS.CANCELLERS[kind]), kind)

    def test_server_py_no_longer_registers_them_itself(self):
        text = (Path(__file__).resolve().parents[1] / "server" / "server.py").read_text(encoding="utf-8")
        for module, resolves, _ in HOSTS + STORAGE_APPS:
            alias = ALIASES[module.__name__]
            for resolve in set(resolves.values()):
                self.assertFalse(f"= {alias}.{resolve.__name__}\n" in text, alias)
            self.assertFalse(f"= ({alias}.cancel_plan, {alias}.cancel_run)" in text, alias)

    def test_an_unraid_import_can_be_cancelled_with_its_cdi_cleanup(self):
        ops = fake_ops()
        UNRAID_VMS.register(ops)
        self.assertEqual({UNRAID_VMS.KIND}, ops.CDI_CANCELLABLE)


if __name__ == "__main__":
    unittest.main()
