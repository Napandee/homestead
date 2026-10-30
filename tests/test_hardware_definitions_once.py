"""The hardware definitions are one ConfigMap read per build (#977).

The Apps list read the homestead-hardware ConfigMap once for each
Deployment, and the Hosts list twice for each node: the same answer every
time, a quarter of the leader's Kubernetes calls."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import server
from fakes.kube import Kube

HW = server.HW
NODES = server.NODES
WORKLOADS = server.WORKLOADS

CORAL = {"id": "coral_usb", "name": "Google Coral USB", "label": "hardware/coral-usb",
         "host_path": "/dev/bus/usb", "container_path": "/dev/bus/usb", "path_type": "Directory",
         "usb_ids": ["1a6e:089a"]}


def deployment(name, **spec):
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": name, "namespace": "lab", "annotations": {}, "labels": {}},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}},
                     "template": {"metadata": {"labels": {"app": name}},
                                  "spec": {"containers": [{"name": name, "image": "example.com/app:1"}], **spec}}},
            "status": {"readyReplicas": 1}}


def node(name):
    return {"apiVersion": "v1", "kind": "Node",
            "metadata": {"name": name, "labels": {}, "annotations": {}}, "spec": {},
            "status": {"capacity": {"cpu": "4", "memory": "8Gi"}, "allocatable": {},
                       "conditions": [{"type": "Ready", "status": "True"}], "nodeInfo": {}, "addresses": []}}


class DefinitionsOnce(unittest.TestCase):
    def setUp(self):
        self.map_path = f"/api/v1/namespaces/{HW.NS}/configmaps/{HW._hardware_map()}"
        self.hardware_map = {"apiVersion": "v1", "kind": "ConfigMap",
                             "metadata": {"name": HW._hardware_map(), "namespace": HW.NS},
                             "data": {"features.json": json.dumps([CORAL])}}
        self.reads = 0
        real = HW.kget

        def counting(path, *args, **kwargs):
            if path == self.map_path:
                self.reads += 1
            return real(path, *args, **kwargs)
        self.enterContext(mock.patch.object(HW, "kget", counting))

    def map_reads_for_apps(self, count):
        apps = [deployment(f"app-{i}", nodeSelector={"hardware/coral-usb": "true"}) for i in range(count)]
        with Kube(self.hardware_map, *apps):
            self.reads = 0
            rows = WORKLOADS.get_workloads()
        self.assertEqual(count, len(rows))
        self.assertTrue(all(row["hardware"] == ["coral_usb"] for row in rows), "the definitions still apply")
        return self.reads

    def test_the_apps_list_reads_the_definitions_as_often_for_fifty_apps_as_for_one(self):
        one = self.map_reads_for_apps(1)
        self.assertEqual(one, self.map_reads_for_apps(50))
        self.assertLessEqual(one, 1)

    def hosts(self, names, temps):
        return mock.patch.multiple(
            NODES, node_temps=lambda **kw: temps, get_app_settings=lambda: {},
            DEFAULT_APP_SETTINGS={"smart": {}}, node_duties=lambda pods, vols: {},
            node_stats=lambda name: {}, workload_names=lambda pods, system: ([], []))

    def test_the_hosts_list_reads_the_definitions_at_most_once(self):
        names = [f"node-{c}" for c in "abcdefgh"]
        temps = {name: {"devices": {"usb": [{"vid": "1a6e", "pid": "089a"}]}} for name in names}
        with Kube(self.hardware_map, *[node(name) for name in names]), self.hosts(names, temps), \
                mock.patch.object(NODES.DISKS, "summary", lambda: {}), \
                mock.patch.object(NODES.HOST_OS, "report", lambda: {"hosts": {}}):
            self.reads = 0
            rows = NODES.get_nodes()
        self.assertEqual(len(names), len(rows))
        self.assertTrue(all(row["hardware"]["coral_usb"] for row in rows), "the definitions still apply")
        self.assertLessEqual(self.reads, 1)

    def test_the_hardware_labels_loop_reads_the_definitions_at_most_once(self):
        names = [f"node-{c}" for c in "abcdefgh"]
        temps = {name: {"devices": {"usb": [{"vid": "1a6e", "pid": "089a"}]}} for name in names}
        with Kube(self.hardware_map, *[node(name) for name in names]) as kube, self.hosts(names, temps):
            self.reads = 0
            found = NODES.reconcile_hardware()
            labelled = kube.object("/api/v1/nodes/node-a")["metadata"]["labels"]
        self.assertEqual({name: ["coral_usb"] for name in names}, found)
        self.assertEqual("true", labelled.get("hardware/coral-usb"))
        self.assertLessEqual(self.reads, 1)

    def test_saved_definitions_show_in_the_next_apps_build(self):
        with Kube(self.hardware_map, deployment("plex", nodeSelector={"hardware/hailo": "true"})):
            self.assertEqual([], WORKLOADS.get_workloads()[0]["hardware"])
            HW.save_features([dict(CORAL), {"id": "hailo", "name": "Hailo-8", "label": "hardware/hailo",
                                            "host_path": "/dev/hailo0", "container_path": "/dev/hailo0",
                                            "path_type": "CharDevice", "usb_ids": []}])
            self.assertEqual(["hailo"], WORKLOADS.get_workloads()[0]["hardware"])


if __name__ == "__main__":
    unittest.main()
