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
        self.assertEqual({"ready": False, "status": "Unknown", "since": "2026-10-17T09:00:00Z",
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

    def test_malformed_object_is_unread_not_keyerror(self):
        self.objects[f"{LH}/replicas"]["items"].append(None)
        with self.assertRaises(sr.Unread) as ctx:
            sr.read_state("lab", "web", "host-1")
        self.assertIn("can't read", str(ctx.exception))

    def test_longhorn_node_without_ready_condition_is_none(self):
        self.objects[f"{LH}/nodes"]["items"][0]["status"] = {"conditions": []}
        self.assertIsNone(sr.read_state("lab", "web", "host-1")["lh_nodes"]["host-1"])

    def test_wait_mode_from_tolerations(self):
        spec = self.objects["/apis/apps/v1/namespaces/lab/statefulsets/web"]["spec"]["template"]["spec"]
        spec["tolerations"] = [{"key": "node.kubernetes.io/unreachable", "operator": "Exists", "effect": "NoExecute"}]
        self.assertTrue(sr.read_state("lab", "web", "host-1")["set"]["wait_mode"])


NOW = 1792231200.0  # 2026-10-17T10:00:00Z
DOWN_SINCE = "2026-10-17T09:30:00Z"  # 1800 s before NOW


def good_state():
    return {"node": {"ready": False, "status": "Unknown", "since": DOWN_SINCE, "lease": "2026-10-17T09:29:00.000000Z"},
            "harvester": False, "set": {"ns": "lab", "name": "web", "wait_mode": False},
            "pods": [{"name": "web-0", "uid": "u0", "node": "host-1", "terminating": False, "claims": ["data-web-0"]}],
            "claims": {"data-web-0": {"access": ["ReadWriteOnce"], "volume": "pv-a", "longhorn": True}},
            "lh_nodes": {"host-1": False, "host-2": True, "host-3": True},
            "replicas": {"pv-a": [{"node": "host-1", "failed": False, "healthy_at": True},
                                  {"node": "host-2", "failed": False, "healthy_at": True}]},
            "engines": {"pv-a": [{"node": "host-1", "state": "stopped"}]},
            "robustness": {"pv-a": "degraded"},
            "nodes_ready": {"host-1": False, "host-2": True, "host-3": True}}


class SetRestartPlanTests(unittest.TestCase):
    def refused(self, st, words, now=NOW):
        p = sr.plan(st, now)
        self.assertFalse(p["ready"])
        self.assertTrue(any(words in b for b in p["blockers"]), p["blockers"])
        return p

    def test_good_state_is_ready(self):
        p = sr.plan(good_state(), NOW)
        self.assertTrue(p["ready"], p["blockers"])
        self.assertEqual([], p["blockers"])
        self.assertEqual("host-1", p["node"])
        self.assertEqual(DOWN_SINCE, p["down_since"])
        self.assertEqual(1800, p["down_seconds"])
        self.assertEqual("2026-10-17T09:29:00.000000Z", p["last_heard"])

    def test_harvester_refuses(self):
        st = good_state(); st["harvester"] = True
        self.refused(st, "Harvester")

    def test_host_ready_refuses(self):
        st = good_state(); st["node"].update(ready=True, since=None)
        self.refused(st, "is Ready")

    def test_down_under_ten_minutes_refuses(self):
        self.refused(good_state(), "10 minutes", now=NOW - 1800 + 599)
        self.assertTrue(sr.plan(good_state(), NOW - 1800 + 600)["ready"])

    def test_unknown_down_time_refuses(self):
        st = good_state(); st["node"]["since"] = None
        self.refused(st, "went down")

    def test_no_pod_on_host_refuses(self):
        st = good_state(); st["pods"] = []
        self.refused(st, "no pod")

    def test_not_longhorn_refuses(self):
        st = good_state(); st["claims"]["data-web-0"]["longhorn"] = False
        p = self.refused(st, "data-web-0")
        self.assertTrue(any("Longhorn" in b for b in p["blockers"]))

    def test_unbound_claim_refuses(self):
        st = good_state(); st["claims"]["data-web-0"].update(volume=None, longhorn=False)
        self.refused(st, "data-web-0")

    def test_rwx_refuses(self):
        st = good_state(); st["claims"]["data-web-0"]["access"] = ["ReadWriteMany"]
        self.refused(st, "ReadWriteMany")

    def test_longhorn_still_sees_host_ready_refuses(self):
        st = good_state(); st["lh_nodes"]["host-1"] = True
        self.refused(st, "Longhorn")

    def test_host_missing_from_longhorn_refuses(self):
        st = good_state(); del st["lh_nodes"]["host-1"]
        self.refused(st, "Longhorn")

    def test_engine_running_on_host_refuses(self):
        st = good_state(); st["engines"]["pv-a"][0]["state"] = "running"
        self.refused(st, "engine")

    def test_only_copy_elsewhere_failed_refuses(self):
        st = good_state(); st["replicas"]["pv-a"][1].update(failed=True, healthy_at=False)
        p = self.refused(st, "pv-a")
        self.assertTrue(any("data-web-0" in b for b in p["blockers"]))

    def test_unsynced_copy_refuses(self):
        st = good_state(); st["replicas"]["pv-a"][1]["healthy_at"] = False
        self.refused(st, "data-web-0")

    def test_copy_on_another_down_host_refuses(self):
        st = good_state()
        st["replicas"]["pv-a"][1]["node"] = "host-3"
        st["nodes_ready"]["host-3"] = False
        self.refused(st, "host-3, which is not Ready either")

    def test_faulted_volume_refuses(self):
        st = good_state(); st["robustness"]["pv-a"] = "faulted"
        self.refused(st, "faulted")

    def test_volume_missing_from_robustness_refuses(self):
        st = good_state(); st["robustness"] = {}
        self.refused(st, "pv-a")

    def test_ready_plan_lists_every_pod_and_volume(self):
        st = good_state()
        st["pods"].append({"name": "web-1", "uid": "u1", "node": "host-1", "terminating": True, "claims": ["data-web-1"]})
        st["claims"]["data-web-1"] = {"access": ["ReadWriteOnce"], "volume": "pv-b", "longhorn": True}
        st["replicas"]["pv-b"] = [{"node": "host-1", "failed": False, "healthy_at": True},
                                  {"node": "host-3", "failed": False, "healthy_at": True}]
        st["engines"]["pv-b"] = []
        st["robustness"]["pv-b"] = "degraded"
        p = sr.plan(st, NOW)
        self.assertTrue(p["ready"], p["blockers"])
        self.assertEqual([("web-0", "u0", False), ("web-1", "u1", True)],
                         [(x["name"], x["uid"], x["terminating"]) for x in p["pods"]])
        self.assertEqual([{"pvc": "data-web-0", "volume": "pv-a", "copy_on": "host-2"},
                          {"pvc": "data-web-1", "volume": "pv-b", "copy_on": "host-3"}], p["volumes"])

    def test_second_pods_bad_volume_blocks_whole_plan(self):
        st = good_state()
        st["pods"].append({"name": "web-1", "uid": "u1", "node": "host-1", "terminating": False, "claims": ["data-web-1"]})
        st["claims"]["data-web-1"] = {"access": ["ReadWriteOnce"], "volume": "pv-b", "longhorn": False}
        self.refused(st, "data-web-1")

    def test_token_changes_with_pod_uid(self):
        st = good_state(); a = sr.plan(st, NOW)["review_token"]
        st["pods"][0]["uid"] = "u0-new"
        self.assertNotEqual(a, sr.plan(st, NOW)["review_token"])

    def test_token_changes_with_chosen_copy(self):
        st = good_state(); a = sr.plan(st, NOW)["review_token"]
        st["replicas"]["pv-a"][1]["node"] = "host-3"
        self.assertNotEqual(a, sr.plan(st, NOW)["review_token"])

    def test_token_stable_for_same_state(self):
        a = sr.plan(good_state(), NOW)["review_token"]
        self.assertEqual(a, sr.plan(good_state(), NOW + 60)["review_token"])
        self.assertEqual(20, len(a))

    def test_longhorn_node_none_or_missing_blocks_false_passes(self):
        for v in (None, True):
            st = good_state(); st["lh_nodes"]["host-1"] = v
            self.refused(st, "Longhorn")
        st = good_state(); del st["lh_nodes"]["host-1"]
        self.refused(st, "Longhorn")
        self.assertTrue(sr.plan(good_state(), NOW)["ready"])

    def test_engine_must_be_stopped(self):
        for state in ("running", "unknown", "error", None, "starting"):
            st = good_state(); st["engines"]["pv-a"][0]["state"] = state
            self.refused(st, "stopped")
        st = good_state(); st["engines"]["pv-a"].append({"node": "host-2", "state": "running"})
        self.assertTrue(sr.plan(st, NOW)["ready"])

    def test_robustness_allowlist(self):
        for ok in ("healthy", "degraded"):
            st = good_state(); st["robustness"]["pv-a"] = ok
            self.assertTrue(sr.plan(st, NOW)["ready"], ok)
        for bad in ("unknown", "faulted", None):
            st = good_state(); st["robustness"]["pv-a"] = bad
            self.refused(st, "pv-a")

    def test_kubelet_still_answering_blocks(self):
        st = good_state(); st["node"]["status"] = "False"
        self.refused(st, "still answering")

    def test_fresh_lease_blocks(self):
        st = good_state(); st["node"]["lease"] = "2026-10-17T09:55:00.000000Z"
        self.refused(st, "heard from")

    def test_missing_lease_blocks(self):
        st = good_state(); st["node"]["lease"] = None
        self.refused(st, "last heard")

    def test_malformed_timestamps_block_not_raise(self):
        st = good_state(); st["node"]["since"] = "garbage"
        self.refused(st, "went down")
        st = good_state(); st["node"]["lease"] = "garbage"
        self.refused(st, "last heard")

    def test_wait_mode_carried(self):
        st = good_state(); st["set"]["wait_mode"] = True
        p = sr.plan(st, NOW)
        self.assertTrue(p["ready"])
        self.assertEqual({"ns": "lab", "name": "web", "wait_mode": True}, p["set"])


if __name__ == "__main__":
    unittest.main()
