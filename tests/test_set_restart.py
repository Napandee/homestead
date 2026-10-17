import copy
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_set_restart as sr

LH = sr.LH
PODS = "/api/v1/namespaces/lab/pods?labelSelector=app%3Dweb"


def pod(name, uid, node="host-1", phase="Running", deleting=False, owner="web", claim="data-web-0"):
    meta = {"name": name, "uid": uid, "ownerReferences": [{"kind": "StatefulSet", "name": owner}]}
    if deleting:
        meta["deletionTimestamp"] = "2026-10-17T10:00:00Z"
    return {"metadata": meta, "status": {"phase": phase}, "spec": {
        "nodeName": node, "volumes": [{"name": "d", "persistentVolumeClaim": {"claimName": claim}}]}}


def ready(status, since="2026-10-17T09:00:00Z"):
    return [{"type": "Ready", "status": status, "lastTransitionTime": since}]


class SetRestartReadTests(unittest.TestCase):
    def setUp(self):
        self.objects = {
            "/api/v1/nodes": {"items": [
                {"metadata": {"name": "host-1"}, "status": {"conditions": ready("Unknown")}},
                {"metadata": {"name": "host-2"}, "status": {"conditions": ready("True")}}]},
            "/apis/coordination.k8s.io/v1/namespaces/kube-node-lease/leases/host-1": {
                "spec": {"renewTime": "2026-10-17T08:59:00.000000Z"}},
            "/apis": {"groups": [{"name": "apps"}, {"name": "longhorn.io"}]},
            "/apis/apps/v1/namespaces/lab/statefulsets/web": {
                "spec": {"selector": {"matchLabels": {"app": "web"}}, "template": {"spec": {}}}},
            PODS: {"items": [pod("web-0", "u0"), pod("web-1", "u1", node="host-2"),
                             pod("other-0", "u9", owner="other")]},
            "/api/v1/namespaces/lab/persistentvolumeclaims/data-web-0": {
                "spec": {"accessModes": ["ReadWriteOnce"], "volumeName": "pv-a"}},
            f"{LH}/nodes": {"items": [
                {"metadata": {"name": "host-1"}, "status": {"conditions": ready("False")}},
                {"metadata": {"name": "host-2"}, "status": {"conditions": ready("True")}}]},
            f"{LH}/replicas": {"items": [
                {"spec": {"nodeID": "host-1", "volumeName": "pv-a", "failedAt": "", "healthyAt": "x"}},
                {"spec": {"nodeID": "host-2", "volumeName": "pv-a", "failedAt": "t", "healthyAt": ""}}]},
            f"{LH}/engines": {"items": [
                {"spec": {"nodeID": "host-1", "volumeName": "pv-a"}, "status": {"currentState": "running"}}]},
            "/apis/longhorn.io/v1beta2/volumes": {"items": [
                {"metadata": {"name": "pv-a"}, "status": {"robustness": "degraded"}}]}}
        sr.bind(self.get, None)

    def get(self, path, *a, **k):
        if path not in self.objects:
            raise urllib.error.HTTPError(path, 404, "not found", {}, None)
        return copy.deepcopy(self.objects[path])

    def refuses(self, path, words):
        del self.objects[path]
        with self.assertRaises(sr.Unread) as ctx:
            sr.read_state("lab", "web", "host-1")
        self.assertIn(words, str(ctx.exception))

    def test_reads_the_whole_state(self):
        st = sr.read_state("lab", "web", "host-1")
        self.assertEqual({"ready": False, "since": "2026-10-17T09:00:00Z",
                          "lease": "2026-10-17T08:59:00.000000Z"}, st["node"])
        self.assertFalse(st["harvester"])
        self.assertEqual({"ns": "lab", "name": "web", "wait_mode": False}, st["set"])
        self.assertEqual({"host-1": False, "host-2": True}, st["nodes_ready"])
        self.assertEqual({"host-1": False, "host-2": True}, st["lh_nodes"])
        self.assertEqual({"data-web-0": {"access": ["ReadWriteOnce"], "volume": "pv-a", "longhorn": True}},
                         st["claims"])
        self.assertEqual([{"node": "host-1", "failed": False, "healthy_at": True},
                          {"node": "host-2", "failed": True, "healthy_at": False}], st["replicas"]["pv-a"])
        self.assertEqual({"pv-a": [{"node": "host-1", "state": "running"}]}, st["engines"])
        self.assertEqual({"pv-a": "degraded"}, st["robustness"])

    def test_missing_lease_is_none_not_unread(self):
        del self.objects["/apis/coordination.k8s.io/v1/namespaces/kube-node-lease/leases/host-1"]
        self.assertIsNone(sr.read_state("lab", "web", "host-1")["node"]["lease"])

    def test_harvester_group_is_seen(self):
        self.objects["/apis"]["groups"].append({"name": "harvesterhci.io"})
        self.assertTrue(sr.read_state("lab", "web", "host-1")["harvester"])

    def test_unreadable_longhorn_replicas_refuses(self):
        self.refuses(f"{LH}/replicas", "replicas")

    def test_unreadable_engines_refuses(self):
        self.refuses(f"{LH}/engines", "engines")

    def test_unreadable_longhorn_node_refuses(self):
        self.refuses(f"{LH}/nodes", "Longhorn nodes")

    def test_unreadable_api_groups_refuses(self):
        self.refuses("/apis", "API groups")

    def test_unreadable_pvc_refuses(self):
        self.refuses("/api/v1/namespaces/lab/persistentvolumeclaims/data-web-0", "data-web-0")

    def test_reads_terminating_and_failed_pods_with_uid(self):
        self.objects[PODS]["items"] = [pod("web-0", "u0", deleting=True),
                                       pod("web-2", "u2", phase="Failed"), pod("web-1", "u1", node="host-2")]
        pods = sr.read_state("lab", "web", "host-1")["pods"]
        self.assertEqual([("web-0", "u0", "host-1", True, ["data-web-0"]),
                          ("web-2", "u2", "host-1", False, ["data-web-0"])],
                         [(p["name"], p["uid"], p["node"], p["terminating"], p["claims"]) for p in pods])

    def test_wait_mode_from_tolerations(self):
        spec = self.objects["/apis/apps/v1/namespaces/lab/statefulsets/web"]["spec"]["template"]["spec"]
        spec["tolerations"] = [{"key": "node.kubernetes.io/unreachable", "operator": "Exists", "effect": "NoExecute"}]
        self.assertTrue(sr.read_state("lab", "web", "host-1")["set"]["wait_mode"])


if __name__ == "__main__":
    unittest.main()
