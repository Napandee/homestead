import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_kube as K
import homestead_multus as MULTUS
import homestead_addons as ADDONS
from fakes.kube import Kube

NAD_GROUP = "k8s.cni.cncf.io"
CHARTS = "/apis/helm.cattle.io/v1/namespaces/kube-system/helmcharts"


def daemonset(**status):
    return {"apiVersion": "apps/v1", "kind": "DaemonSet",
            "metadata": {"namespace": "kube-system", "name": "multus", "uid": "ds1", "generation": 2},
            "status": {"desiredNumberScheduled": 2, "numberAvailable": 2,
                       "updatedNumberScheduled": 2, "observedGeneration": 2, **status}}


class MultusTests(unittest.TestCase):
    """On the shared fake API (tests/fakes/kube.py): the network attachment API
    is there unless its group is absent, and Multus runs as one DaemonSet."""

    def cluster(self, *objects, agents=True, **status):
        return self.enterContext(Kube(*((daemonset(**status),) if agents else ()), *objects, groups=(NAD_GROUP,)))

    def test_ready_requires_api_and_current_agents(self):
        kube = self.cluster()
        self.assertTrue(MULTUS.inspect(K.kget)["ready"])
        kube.absent_groups.add(NAD_GROUP)
        self.assertFalse(MULTUS.inspect(K.kget)["ready"])

    def test_successful_chart_or_crd_alone_is_not_ready(self):
        self.cluster(agents=False)
        result = MULTUS.inspect(K.kget)
        self.assertTrue(result["installed"])
        self.assertFalse(result["ready"])
        self.assertEqual("unknown", result["state"])

    def test_stale_generation_is_not_ready(self):
        self.cluster(observedGeneration=1)
        self.assertFalse(MULTUS.inspect(K.kget)["ready"])

    def test_zero_nodes_are_not_ready(self):
        self.cluster(desiredNumberScheduled=0, numberAvailable=0)
        self.assertFalse(MULTUS.inspect(K.kget)["ready"])

    def test_crashing_agents_are_reported(self):
        self.cluster({"apiVersion": "v1", "kind": "Pod", "metadata": {
            "namespace": "kube-system", "name": "multus-broken", "ownerReferences": [{"uid": "ds1"}]},
            "status": {"containerStatuses": [{"state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}},
            numberAvailable=1)
        result = MULTUS.inspect(K.kget)
        self.assertIn("multus-broken: CrashLoopBackOff", result["issues"])
        self.assertEqual((1, 2), (result["available"], result["desired"]))

    def test_permission_failure_is_unknown_not_absent(self):
        kube = self.cluster()
        kube.fail(MULTUS.NAD_LIST, 403, times=2)   # one read in inspect, one in _ensure_multus_crd
        self.assertEqual("unknown", MULTUS.inspect(K.kget)["state"])
        with patch.object(ADDONS, "kget", K.kget):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                ADDONS._ensure_multus_crd()
        self.assertEqual(403, caught.exception.code)

    def test_repair_is_scoped_and_preserves_metadata_and_version(self):
        old = ADDONS.K3S_MULTUS_VALUES.replace("    multusAutoconfigDir: /var/lib/rancher/k3s/agent/etc/cni/net.d\n", "")
        chart = {"apiVersion": "helm.cattle.io/v1", "kind": "HelmChart",
                 "metadata": {"namespace": "kube-system", "name": "multus", "resourceVersion": "7",
                              "labels": {"homestead.io/managed": "true"}},
                 "spec": {"chart": "rke2-multus", "repo": ADDONS.RKE2_CHARTS, "targetNamespace": "kube-system",
                          "version": "v4.3.102", "valuesContent": old}}
        kube = self.cluster(chart)
        with patch.object(ADDONS, "kget", K.kget), patch.object(ADDONS, "ksend", K.ksend), \
                patch.object(ADDONS, "platform", return_value={"distribution": "k3s", "helm_controller": True}):
            ADDONS.repair_multus()
        method, path, body, _ = kube.writes[-1]
        self.assertEqual(("PUT", f"{CHARTS}/multus"), (method, path))
        self.assertEqual("7", body["metadata"]["resourceVersion"])
        self.assertEqual("v4.3.102", body["spec"]["version"])
        self.assertIn("multusAutoconfigDir", body["spec"]["valuesContent"])
        self.assertIn("multusAutoconfigDir", kube.object(f"{CHARTS}/multus")["spec"]["valuesContent"])
        chart["spec"]["valuesContent"] += "custom: true\n"
        self.assertFalse(ADDONS._repairable_multus(chart, {"distribution": "k3s"}))

    def test_job_times_out_instead_of_installing_forever(self):
        self.cluster()
        with patch.object(ADDONS, "status", return_value={"multus": {
                "ready": False, "installed": True, "issues": [], "detail": "not ready"}}), \
                patch.object(ADDONS, "kget", K.kget):
            result = ADDONS.multus_progress({"started_at": "2000-01-01T00:00:00Z"})
        self.assertEqual("failed", result[0])

    def test_diagnostics_include_missing_crd_and_node_agent(self):
        command = ADDONS.diagnostic_command("k3s")
        self.assertIn("logs -l app=rke2-multus", command)
        self.assertIn("--previous", command)


if __name__ == "__main__":
    unittest.main()
