"""homestead_lifecycle split by feature (#623): editing a workload lives in
homestead_workload_edit and the node actions in homestead_node_actions, and
homestead_lifecycle keeps the bound state, moving apps and VMs, the routes and a
re-export of every name, so other modules, server.py and tests keep working."""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

import homestead_lifecycle as LIFECYCLE
import test_maintenance_safety as MAINTENANCE_SAFETY
import homestead_node_actions as NODE_ACTIONS
import homestead_workload_edit as WORKLOAD_EDIT

PARTS = (WORKLOAD_EDIT, NODE_ACTIONS)


class LifecycleModuleTests(unittest.TestCase):
    def test_every_moved_name_is_the_same_object_on_homestead_lifecycle(self):
        for part in PARTS:
            moved = [name for name in vars(part) if not name.startswith("__")
                     and getattr(vars(part)[name], "__module__", part.__name__) == part.__name__
                     and name != "L" and not isinstance(vars(part)[name], type(sys))]
            self.assertTrue(moved, part.__name__)
            for name in moved:
                self.assertIs(getattr(LIFECYCLE, name), getattr(part, name), f"{part.__name__}.{name}")

    def test_each_module_can_be_imported_first(self):
        for name in ("homestead_workload_edit", "homestead_node_actions", "homestead_lifecycle"):
            done = subprocess.run([sys.executable, "-c", f"import {name}, homestead_lifecycle as L; L.edit_workload; L.node_power"],
                                  cwd=SERVER, capture_output=True, text=True)
            self.assertEqual(0, done.returncode, f"importing {name} first: {done.stderr}")

    def test_the_routes_are_unchanged(self):
        self.assertEqual({("POST", "/api/node/cordon"), ("GET", "/api/quorum")}, set(LIFECYCLE.ROUTES))
        for part in PARTS:
            self.assertFalse(hasattr(part, "ROUTES"), part.__name__)

    def test_a_client_set_on_homestead_lifecycle_reaches_the_moved_code(self):
        # server.py binds kget on homestead_lifecycle, and tests replace it there.
        asked = []
        with mock.patch.object(LIFECYCLE, "kget", side_effect=lambda path: asked.append(path) or {"items": []}):
            NODE_ACTIONS.quorum_report()
            WORKLOAD_EDIT._pvc_exists("lab", "data")
        self.assertEqual(["/api/v1/nodes", "/api/v1/namespaces/lab/persistentvolumeclaims/data"], asked)

    def test_a_name_replaced_on_homestead_lifecycle_reaches_the_moved_code(self):
        # Tests replace seed_configs on homestead_lifecycle; editing reads it there.
        requested = [{"init_container": "seed", "config_map": "config", "key": "config", "value": "new"}]
        with mock.patch.object(LIFECYCLE, "seed_configs", return_value=[]) as seeds, \
                self.assertRaisesRegex(ValueError, "not attached"):
            WORKLOAD_EDIT._prepare_seed_configs("lab", {"metadata": {"name": "app"}}, requested)
        seeds.assert_called_once()


class DrainWaitTests(unittest.TestCase):
    # The fixtures of test_maintenance_safety: a host with a Longhorn instance manager.
    get = MAINTENANCE_SAFETY.MaintenanceSafetyTests.get
    setUp = MAINTENANCE_SAFETY.MaintenanceSafetyTests.setUp
    with_longhorn_manager = MAINTENANCE_SAFETY.MaintenanceSafetyTests.with_longhorn_manager
    with_budget = MAINTENANCE_SAFETY.MaintenanceSafetyTests.with_budget

    def test_a_drain_wait_set_on_homestead_lifecycle_is_used(self):
        self.with_longhorn_manager()
        original = MAINTENANCE_SAFETY.power.plan("node1", "reboot")
        def send(method, path, body):
            if body["metadata"]["name"] == "instance-manager-test":
                raise MAINTENANCE_SAFETY.urllib.error.HTTPError(path, 429, "last replica", {}, None)
            self.objects["/api/v1/pods"]["items"] = [self.objects["/api/v1/pods"]["items"][0]]
        with mock.patch.object(LIFECYCLE, "LONGHORN_DRAIN_WAIT", 180), \
                mock.patch.object(LIFECYCLE, "NODE_POWER_ENABLED", True), \
                mock.patch.object(LIFECYCLE, "node_action_check", return_value=(True, "", {})), \
                mock.patch.object(LIFECYCLE, "set_cordon"), \
                mock.patch.object(LIFECYCLE, "kget", side_effect=self.get), \
                mock.patch.object(LIFECYCLE, "ksend", side_effect=send), \
                mock.patch.object(LIFECYCLE.time, "monotonic", side_effect=[0, 121, 181]), \
                mock.patch.object(LIFECYCLE.time, "sleep"), \
                mock.patch.object(LIFECYCLE, "_send_power") as helper:
            with self.assertRaisesRegex(ValueError, "after 3 minutes. Longhorn still prevents eviction"):
                LIFECYCLE.node_power("node1", "reboot", reviewed_pods=original["drain_pods"], before_send=lambda: None)
        helper.assert_not_called()


if __name__ == "__main__":
    unittest.main()
