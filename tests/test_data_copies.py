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
import homestead_lhcapacity as LHCAP

LH_NODES = PARITY.LH_NODES
GiB = 1024 ** 3


def node(name, ready=True):
    return {"metadata": {"name": name}, "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


def lh_node(name, ready=True):
    return {"metadata": {"name": name}, "spec": {"allowScheduling": True, "disks": {"d": {"allowScheduling": True}}},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


def volume(copies, robustness="degraded", size=2 * GiB, unschedulable=True):
    """unschedulable: Longhorn's own proof that a copy cannot be placed - its
    Scheduled condition False, reason ReplicaSchedulingFailure."""
    scheduled = ({"type": "Scheduled", "status": "False", "reason": "ReplicaSchedulingFailure",
                  "message": "replica scheduling failed"} if unschedulable else
                 {"type": "Scheduled", "status": "True", "reason": ""})
    return {"metadata": {"name": "pvc-0001"}, "spec": {"numberOfReplicas": copies, "size": str(size)},
            "status": {"robustness": robustness, "conditions": [scheduled]}}


ENGINES = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/engines?labelSelector=longhornvolume%3Dpvc-0001"


def engines(*modes):
    return {"items": [{"spec": {"volumeName": "pvc-0001"},
                       "status": {"replicaModeMap": {f"pvc-0001-r-{i}": mode for i, mode in enumerate(modes)}}}]}


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
            ENGINES: engines("RW"),
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
        self.assertEqual([("PATCH", f"{PARITY.LH_VOLUMES}/pvc-0001",
                           {"metadata": {"annotations": {"homestead.io/one-host-copies": "3"}},
                            "spec": {"numberOfReplicas": 1}})], cluster.sent)
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

    def test_degraded_alone_is_not_proof(self):
        # Soft anti-affinity on one host with several disks: after a reboot the
        # volume reads degraded while its copies rebuild, and Longhorn can
        # place them. Only a copy Longhorn cannot schedule is proof.
        cluster = Cluster([node("node-a")], [lh_node("node-a")])
        cluster.objects[f"{PARITY.LH_VOLUMES}/pvc-0001"] = volume(3, unschedulable=False)
        self.bind(cluster)
        self.assertIsNone(COPIES.fit_single_host())
        self.assertEqual([], cluster.sent)

    def test_a_rebuilding_copy_lowers_nothing(self):
        cluster = Cluster([node("node-a")], [lh_node("node-a")])
        cluster.objects[ENGINES] = engines("RW", "WO")
        self.bind(cluster)
        self.assertIsNone(COPIES.fit_single_host())
        self.assertEqual([], cluster.sent)

    def test_unreadable_engines_lower_nothing(self):
        cluster = Cluster([node("node-a")], [lh_node("node-a")])
        cluster.fail = {ENGINES}
        self.bind(cluster)
        self.assertIsNone(COPIES.fit_single_host())
        self.assertEqual([], cluster.sent)

    def test_a_copy_longhorn_cannot_place_is_proof(self):
        # The older map form of Longhorn's conditions counts too.
        cluster = Cluster([node("node-a")], [lh_node("node-a")])
        cluster.objects[f"{PARITY.LH_VOLUMES}/pvc-0001"]["status"]["conditions"] = {
            "scheduled": {"type": "Scheduled", "status": "False", "reason": "ReplicaSchedulingFailure"}}
        self.bind(cluster)
        self.assertTrue(COPIES.fit_single_host())

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


def lh_store(name, room_gib=20, ready=True):
    """A Longhorn node with one disk and room_gib of room left on it."""
    found = lh_node(name, ready)
    found["status"]["diskStatus"] = {"d": {"storageMaximum": 40 * GiB, "storageAvailable": 40 * GiB,
                                           "storageScheduled": (40 - room_gib) * GiB}}
    return found


R2 = "/apis/storage.k8s.io/v1/storageclasses/longhorn-r2"


def r2_class(replicas="2", migratable="false", provisioner="driver.longhorn.io"):
    return {"metadata": {"name": "longhorn-r2"}, "provisioner": provisioner,
            "parameters": {"numberOfReplicas": replicas, "migratable": migratable}}


class FactsTests(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()

    def cluster(self, names=("node-a", "node-b"), copies=1, rooms=None, distribution="kubeadm", harvester=False,
                longhorn=True, storage_class="longhorn", extra=None):
        rooms = rooms or [20] * len(names)
        cluster = Cluster([node(n) for n in names], [lh_store(n, r) for n, r in zip(names, rooms)], copies=copies,
                          robustness="healthy", storage_class=storage_class, extra=extra)
        hostrun = type("H", (), {"run": staticmethod(lambda *a, **k: ("", ""))})
        PARITY.bind(cluster.kget, cluster.ksend, hostrun,
                    lambda force=False: {"distribution": distribution, "longhorn": longhorn, "harvester": harvester},
                    lambda: {}, self.data, ("lab", "homestead"))
        LHCAP.bind(cluster.kget, cluster.ksend, lambda: {})
        return cluster

    def test_facts_single_host(self):
        self.cluster(names=("node-a",))
        facts = COPIES.facts()
        self.assertEqual("single-host", facts["state"])
        self.assertEqual(1, facts["copies"])
        self.assertEqual(1, facts["storage_hosts"])

    def test_facts_can_keep_two(self):
        cluster = self.cluster()
        facts = COPIES.facts()
        self.assertEqual("can-keep-two", facts["state"])
        self.assertEqual({"claim": "homestead-data", "storage_class": "longhorn", "copies": 1, "storage_hosts": 2,
                          "room_hosts": 2, "size_gb": 2.0},
                         {k: facts[k] for k in ("claim", "storage_class", "copies", "storage_hosts", "room_hosts", "size_gb")})
        self.assertEqual({"exists": False, "replicas": 0, "migratable": False, "longhorn": False, "usable": True, "why": ""},
                         facts["r2"])
        self.assertEqual([], cluster.sent, "facts only read")

    def test_facts_rising_on_k3s(self):
        # Review focus 4: own_copies() raises it within a minute; nothing to offer.
        for distribution in ("k3s", "rke2"):
            with self.subTest(distribution=distribution):
                self.cluster(distribution=distribution)
                self.assertTrue(PARITY.own_copies_due(PARITY._storage_hosts(PARITY._ready_nodes())))
                self.assertEqual("rising", COPIES.facts()["state"])

    def test_own_copies_due_matches_own_copies(self):
        cluster = self.cluster(distribution="k3s")
        storing = PARITY._storage_hosts(PARITY._ready_nodes())
        self.assertTrue(PARITY.own_copies_due(storing))
        self.assertEqual([], cluster.sent, "a pure read")
        self.assertEqual([("homestead-data", 2)], PARITY.own_copies(storing))
        cluster.objects[f"{PARITY.LH_VOLUMES}/pvc-0001"]["spec"].update(cluster.sent[-1][2]["spec"])
        self.assertFalse(PARITY.own_copies_due(storing), "raised: nothing left to do")

    def test_own_copies_due_not_on_other_distributions_or_harvester(self):
        for kwargs in ({"distribution": "kubeadm"}, {"distribution": "k3s", "harvester": True}):
            with self.subTest(**kwargs):
                self.cluster(**kwargs)
                self.assertFalse(PARITY.own_copies_due(PARITY._storage_hosts(PARITY._ready_nodes())))

    def test_facts_no_room(self):
        self.cluster(rooms=[20, 1])
        facts = COPIES.facts()
        self.assertEqual("single-host", facts["state"])
        self.assertEqual(1, facts["room_hosts"])

    def test_a_host_longhorn_cannot_store_on_does_not_count(self):
        cluster = self.cluster(names=("node-a", "node-b"))
        cluster.objects[LH_NODES]["items"][1]["status"]["conditions"][0]["status"] = "False"
        self.assertEqual("single-host", COPIES.facts()["state"])

    def test_facts_two_copies_ok(self):
        self.cluster(copies=2)
        self.assertEqual("ok", COPIES.facts()["state"])

    def test_facts_harvester_and_local_path_na(self):
        self.cluster(harvester=True)
        self.assertEqual("n/a", COPIES.facts()["state"])
        self.cluster(longhorn=False)
        self.assertEqual("n/a", COPIES.facts()["state"])
        cluster = self.cluster(storage_class="local-path")
        del cluster.objects[f"{PARITY.LH_VOLUMES}/pvc-0001"]
        self.assertEqual("n/a", COPIES.facts()["state"])

    def test_facts_unreadable_is_na(self):
        cluster = self.cluster()
        cluster.fail = {LH_NODES}
        self.assertEqual("n/a", COPIES.facts()["state"])

    def test_facts_r2_description(self):
        cases = ((r2_class(), True, ""),
                 (r2_class(migratable="true"), False, "migratable"),
                 (r2_class(replicas="1"), False, "1 copy"),
                 (r2_class(provisioner="rancher.io/local-path"), False, "not a Longhorn class"))
        for found, usable, why in cases:
            with self.subTest(found=found):
                self.cluster(extra={R2: found})
                r2 = COPIES.facts()["r2"]
                self.assertTrue(r2["exists"])
                self.assertEqual(usable, r2["usable"])
                self.assertIn(why, r2["why"])


class SelfHealthTests(unittest.TestCase):
    def test_health_carries_data_copies(self):
        from unittest import mock
        import homestead_self_health as SELF_HEALTH
        fake = {"state": "can-keep-two", "copies": 1}
        with mock.patch.object(COPIES, "facts", lambda: fake):
            self.assertEqual(fake, SELF_HEALTH.data_copies())

    def test_health_says_na_when_facts_fail(self):
        from unittest import mock
        import homestead_self_health as SELF_HEALTH

        def broken():
            raise RuntimeError("Longhorn did not answer")
        with mock.patch.object(COPIES, "facts", broken):
            self.assertEqual({"state": "n/a", "error": "Longhorn did not answer"}, SELF_HEALTH.data_copies())


if __name__ == "__main__":
    unittest.main()
