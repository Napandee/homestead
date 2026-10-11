"""A host page's apps carry their namespace (#366), so moving one from there
names the right app instead of assuming "lab"."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_nodes as NODES


def pod(ns, name, app=None, labels=None):
    meta = {"namespace": ns, "name": name, "labels": {**({"app": app} if app else {}), **(labels or {})}}
    return {"metadata": meta, "spec": {"nodeName": "k1"}}


class WorkloadNamesTests(unittest.TestCase):
    def test_names_and_ids_with_their_namespace(self):
        names, ids = NODES.workload_names([pod("lab", "ha-abc-1", app="ha"), pod("media", "plex-abc-1"),
                                           pod("kube-system", "coredns-x-1", app="coredns")], {"kube-system"})
        self.assertEqual(["ha", "plex"], names)
        self.assertEqual(["lab/ha", "media/plex"], ids)

    def test_the_same_app_in_two_namespaces_is_two_ids(self):
        names, ids = NODES.workload_names([pod("lab", "web-a-1", app="web"), pod("test", "web-b-1", app="web")], set())
        self.assertEqual(["web"], names)
        self.assertEqual(["lab/web", "test/web"], ids)


    def test_the_id_names_the_deployment_not_its_app_label(self):
        # A chart's app label is often a component name: the move looks up the Deployment.
        p = pod("lab", "frontend-7d9f8-x2k", app="web", labels={"pod-template-hash": "7d9f8"})
        p["metadata"]["ownerReferences"] = [{"kind": "ReplicaSet", "name": "frontend-7d9f8", "controller": True}]
        names, ids = NODES.workload_names([p], set())
        self.assertEqual(["lab/frontend"], ids)
        self.assertEqual(["web"], names)

    def test_a_statefulset_pod_names_its_statefulset(self):
        p = pod("lab", "db-0", app="db")
        p["metadata"]["ownerReferences"] = [{"kind": "StatefulSet", "name": "postgres", "controller": True}]
        self.assertEqual(["lab/postgres"], NODES.workload_names([p], set())[1])


if __name__ == "__main__":
    unittest.main()
