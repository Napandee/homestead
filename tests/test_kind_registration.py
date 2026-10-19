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


def fake_ops():
    return SimpleNamespace(RESOLVERS={}, CANCELLERS={}, RESUMABLE={}, LOGGERS={}, CLEANUPS=set(), CDI_CANCELLABLE=set())


class HostKindTests(unittest.TestCase):
    def test_each_host_module_registers_its_kinds(self):
        for module, resolves, cancels in HOSTS:
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
        for module, resolves, cancels in HOSTS:
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
        for module, resolves, _ in HOSTS:
            alias = {"homestead_host_os": "HOST_OS", "homestead_os_rollout": "OS_ROLLOUT", "homestead_host_bridge": "HOST_BRIDGE",
                     "homestead_host_bonds": "HOST_BONDS", "homestead_uplinks": "UPLINKS", "homestead_host_console": "HOST_CONSOLE",
                     "homestead_k3scluster": "K3SC", "homestead_power": "POWER"}[module.__name__]
            self.assertNotIn(f"= {alias}.status", text, alias)


if __name__ == "__main__":
    unittest.main()
