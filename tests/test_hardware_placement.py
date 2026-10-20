import sys
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import homestead_hardware as hardware
import homestead_kube as K
import homestead_place as place
from fakes.kube import Kube


CORAL = {
    "id": "coral_usb", "name": "Google Coral USB",
    "label": "hardware/coral-usb", "host_path": "/dev/bus/usb",
    "container_path": "/dev/bus/usb", "path_type": "Directory",
    "usb_ids": ["1a6e:089a"], "builtin": False,
}


class HardwarePlacementTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(place, "hardware_features", lambda: [CORAL]))
        self.nodes = [
            {"name": "node1", "status": "Ready", "schedulable": True,
             "hardware": {"coral_usb": True}, "labels": {}, "allocatable": {},
             "temps": {"devices": {"usb": [{"vid": "1a6e", "pid": "089a"}]}}},
            {"name": "node2", "status": "Ready", "schedulable": True,
             "hardware": {"coral_usb": False}, "labels": {}, "allocatable": {},
             "temps": {"devices": {"usb": []}}},
        ]
        self.enterContext(mock.patch.object(place, "get_nodes", lambda: self.nodes))
        self.dep = {
            "spec": {"template": {"spec": {
                "containers": [{"name": "frigate"}],
                "nodeSelector": {"hardware/coral-usb": "true"},
                "volumes": [{"name": "coral", "hostPath": {"path": "/dev/bus/usb"}}],
            }}}
        }

        # On the shared fake API (tests/fakes/kube.py).
        self.kube = self.enterContext(Kube(
            {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "frigate-1", "namespace": "lab", "labels": {"app": "frigate"}},
             "spec": {"nodeName": "node1"}},
            {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "frigate", "namespace": "lab"}, **self.dep}))
        self.read = set()

        def spy(path, *args, **kwargs):
            self.read.add(path)
            return K.kget(path, *args, **kwargs)
        self.enterContext(mock.patch.object(place, "kget", spy))

    def assertReadOnlyThePodsAndTheDeployment(self):
        self.assertEqual({"/api/v1/pods", "/apis/apps/v1/namespaces/lab/deployments/frigate"}, self.read)

    def test_usb_inventory_matches_vid_pid(self):
        self.enterContext(mock.patch.object(hardware, "features", lambda: [CORAL]))
        result = hardware.inventory({}, {"usb": [{"vid": "1a6e", "pid": "089a"}]})
        self.assertTrue(result[0]["detected"])
        self.assertTrue(result[0]["available"])

    def test_node_impact_marks_workload_stranded(self):
        result = place.impact("node1")
        self.assertFalse(result["safe"])
        self.assertEqual(["frigate"], [x["name"] for x in result["stranded"]])
        self.assertReadOnlyThePodsAndTheDeployment()

    def test_node_impact_lists_compatible_destination(self):
        self.nodes[1]["hardware"]["coral_usb"] = True
        result = place.impact("node1")
        self.assertTrue(result["safe"])
        self.assertEqual(["node2"], result["movable"][0]["eligible"])
        self.assertReadOnlyThePodsAndTheDeployment()


if __name__ == "__main__":
    unittest.main()
