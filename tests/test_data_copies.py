"""#731 item 3: Homestead's own data volume on a one-host Longhorn cluster.

Stock Longhorn's `longhorn` class asks for three copies, and one host holds
one, so the volume reads degraded and every warning built on that says so.
homestead_data_copies lowers only Homestead's own data volume, only on a
cluster with exactly one Kubernetes node and one Longhorn node."""
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_node_parity as PARITY
import homestead_data_copies as COPIES

LH_NODES = PARITY.LH_NODES
GiB = 1024 ** 3


def node(name, ready=True):
    return {"metadata": {"name": name}, "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


def lh_node(name, ready=True):
    return {"metadata": {"name": name}, "spec": {"allowScheduling": True, "disks": {"d": {"allowScheduling": True}}},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


def volume(copies, robustness="degraded", size=2 * GiB):
    return {"metadata": {"name": "pvc-0001"}, "spec": {"numberOfReplicas": copies, "size": str(size)},
            "status": {"robustness": robustness}}


class Cluster:
    def __init__(self, nodes, lh_nodes, copies=3, robustness="degraded", storage_class="longhorn", extra=None):
        deployment = {"spec": {"template": {"spec": {"volumes": [
            {"name": "data", "persistentVolumeClaim": {"claimName": "homestead-data"}},
            {"name": "tmp", "emptyDir": {}}]}}}}
        self.objects = {
            "/api/v1/nodes": {"items": nodes},
            LH_NODES: {"items": lh_nodes},
            "/apis/apps/v1/namespaces/lab/deployments/homestead": deployment,
            "/api/v1/namespaces/lab/persistentvolumeclaims/homestead-data": {
                "spec": {"volumeName": "pvc-0001", "storageClassName": storage_class,
                         "resources": {"requests": {"storage": "2Gi"}}}},
            f"{PARITY.LH_VOLUMES}/pvc-0001": volume(copies, robustness),
            f"{PARITY.LH_VOLUMES}/pvc-0002": volume(3),
            **(extra or {})}
        self.sent = []
        self.fail = set()

    def kget(self, path):
        if path in self.fail:
            raise KeyError(path)
        if path not in self.objects:
            raise urllib.error.HTTPError(path, 404, "not found", {}, None)
        return self.objects[path]

    def ksend(self, method, path, body=None, ctype=""):
        self.sent.append((method, path, body))


class FitSingleHostTests(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()

    def bind(self, cluster, distribution="kubeadm", longhorn=True, harvester=False):
        hostrun = type("H", (), {"run": staticmethod(lambda *a, **k: ("", ""))})
        PARITY.bind(cluster.kget, cluster.ksend, hostrun,
                    lambda force=False: {"distribution": distribution, "longhorn": longhorn, "harvester": harvester},
                    lambda: {}, self.data, ("lab", "homestead"))

    def test_one_host_stock_longhorn_lowers_own_volume_to_one(self):
        cluster = Cluster([node("node-a")], [lh_node("node-a")])
        self.bind(cluster)
        line = COPIES.fit_single_host()
        self.assertEqual("Homestead's data volume homestead-data keeps 1 copy: this cluster has one host", line)
        self.assertEqual([("PATCH", f"{PARITY.LH_VOLUMES}/pvc-0001", {"spec": {"numberOfReplicas": 1}})], cluster.sent)
        self.assertEqual(1, PARITY._load()["own_copies"])

    def test_other_volumes_and_settings_untouched(self):
        cluster = Cluster([node("node-a")], [lh_node("node-a")])
        self.bind(cluster)
        COPIES.fit_single_host()
        paths = [path for _, path, _ in cluster.sent]
        self.assertNotIn(f"{PARITY.LH_VOLUMES}/pvc-0002", paths)
        self.assertFalse([p for p in paths if "settings" in p or "storageclasses" in p or "helmcharts" in p], paths)

    def test_any_distribution(self):
        for distribution in ("k3s", "rke2", "kubeadm", "talos", ""):
            with self.subTest(distribution=distribution):
                cluster = Cluster([node("node-a")], [lh_node("node-a")])
                self.bind(cluster, distribution=distribution)
                self.assertTrue(COPIES.fit_single_host())

    def test_two_k8s_nodes_one_longhorn_node_lowers_nothing(self):
        # A tiebreaker, or an agent Longhorn does not run on: still two hosts.
        cluster = Cluster([node("node-a"), node("node-b")], [lh_node("node-a")])
        self.bind(cluster)
        self.assertIsNone(COPIES.fit_single_host())
        self.assertEqual([], cluster.sent)

    def test_one_ready_of_three_lowers_nothing(self):
        # Review focus 1: hosts down or rebooting are still hosts.
        cluster = Cluster([node("node-a"), node("node-b", ready=False), node("node-c", ready=False)],
                          [lh_node("node-a"), lh_node("node-b", ready=False), lh_node("node-c", ready=False)])
        self.bind(cluster)
        self.assertIsNone(COPIES.fit_single_host())
        self.assertEqual([], cluster.sent)

    def test_one_k8s_node_two_longhorn_records_lowers_nothing(self):
        cluster = Cluster([node("node-a")], [lh_node("node-a"), lh_node("node-b", ready=False)])
        self.bind(cluster)
        self.assertIsNone(COPIES.fit_single_host())
        self.assertEqual([], cluster.sent)

    def test_harvester_and_no_longhorn_do_nothing(self):
        for kwargs in ({"harvester": True}, {"longhorn": False}):
            with self.subTest(**kwargs):
                cluster = Cluster([node("node-a")], [lh_node("node-a")])
                self.bind(cluster, **kwargs)
                self.assertIsNone(COPIES.fit_single_host())
                self.assertEqual([], cluster.sent)

    def test_already_one_does_nothing(self):
        cluster = Cluster([node("node-a")], [lh_node("node-a")], copies=1, robustness="healthy")
        self.bind(cluster)
        self.assertIsNone(COPIES.fit_single_host())
        self.assertEqual([], cluster.sent)

    def test_a_healthy_volume_keeps_its_copies(self):
        # Copies the one host does hold (soft anti-affinity, several disks)
        # are not thrown away: there is nothing to fix.
        cluster = Cluster([node("node-a")], [lh_node("node-a")], copies=3, robustness="healthy")
        self.bind(cluster)
        self.assertIsNone(COPIES.fit_single_host())
        self.assertEqual([], cluster.sent)

    def test_longhorn_unreadable_does_nothing(self):
        for path in ("/api/v1/nodes", LH_NODES, f"{PARITY.LH_VOLUMES}/pvc-0001",
                     "/apis/apps/v1/namespaces/lab/deployments/homestead"):
            with self.subTest(path=path):
                cluster = Cluster([node("node-a")], [lh_node("node-a")])
                cluster.fail = {path}
                self.bind(cluster)
                self.assertIsNone(COPIES.fit_single_host())
                self.assertEqual([], cluster.sent)

    def test_own_data_volumes_describes_the_claim(self):
        cluster = Cluster([node("node-a")], [lh_node("node-a")])
        self.bind(cluster)
        [found] = COPIES.own_data_volumes()
        self.assertEqual("homestead-data", found["claim"])
        self.assertEqual("pvc-0001", found["pv"])
        self.assertEqual("longhorn", found["storage_class"])
        self.assertEqual(2.0, found["size_gb"])
        self.assertEqual(3, found["volume"]["spec"]["numberOfReplicas"])

    def test_own_data_volumes_without_longhorn_volume(self):
        cluster = Cluster([node("node-a")], [lh_node("node-a")], storage_class="local-path")
        del cluster.objects[f"{PARITY.LH_VOLUMES}/pvc-0001"]
        self.bind(cluster)
        [found] = COPIES.own_data_volumes()
        self.assertIsNone(found["volume"])
        self.assertEqual(2.0, found["size_gb"])


if __name__ == "__main__":
    unittest.main()
