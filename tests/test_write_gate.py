"""The fence on every Kubernetes write, tested through a real write (#595).

Other tests patch authorize_workload_write and the self-data fence out, or
check only the order the guards run in. These send writes through K.send on
the shared fake with the guards in place, as the app does, and look at what
reached the API server."""
import contextlib
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_host_access as HOST
import homestead_kube as K
from fakes.kube import Kube

DEPLOYMENTS = "/apis/apps/v1/namespaces/{ns}/deployments"
CONFIGMAPS = "/api/v1/namespaces/lab/configmaps"


def deployment(name, ns="lab", **pod):
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": ns},
            "spec": {"selector": {"matchLabels": {"app": name}},
                     "template": {"metadata": {"labels": {"app": name}},
                                  "spec": {"containers": [{"name": name, "image": "example.com/app:1"}], **pod}}}}


class WriteGateTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(HOST.bind_targets, HOST._system_namespaces)
        HOST.bind_targets({"kube-system"})
        self.addCleanup(HOST.set_role, None)

    def written(self, kube, method):
        return [(m, path) for m, path, *_ in kube.writes if m == method]

    def test_an_operator_cannot_change_a_host_privileged_workload(self):
        with Kube(deployment("router", hostNetwork=True)) as kube:
            HOST.set_role("operator")
            with self.assertRaisesRegex(PermissionError, "only an admin"):
                K.send("PATCH", DEPLOYMENTS.format(ns="lab") + "/router", {"spec": {"replicas": 0}},
                       ctype="application/merge-patch+json")
            self.assertEqual([], kube.writes, "nothing reached the API server")
            self.assertNotIn("replicas", kube.object(DEPLOYMENTS.format(ns="lab") + "/router")["spec"])

    def test_an_operator_cannot_create_a_privileged_workload(self):
        privileged = deployment("tools")
        privileged["spec"]["template"]["spec"]["containers"][0]["securityContext"] = {"privileged": True}
        with Kube() as kube:
            HOST.set_role("operator")
            with self.assertRaises(PermissionError):
                K.send("POST", DEPLOYMENTS.format(ns="lab"), privileged)
            self.assertEqual([], kube.writes)

    def test_an_operator_cannot_operate_a_workload_in_a_system_namespace(self):
        with Kube(deployment("coredns", ns="kube-system")) as kube:
            HOST.set_role("operator")
            with self.assertRaises(PermissionError):
                K.send("DELETE", DEPLOYMENTS.format(ns="kube-system") + "/coredns")
            self.assertEqual([], kube.writes)
            self.assertIsNotNone(kube.object(DEPLOYMENTS.format(ns="kube-system") + "/coredns"))

    def test_an_operator_can_change_an_ordinary_workload(self):
        with Kube(deployment("frigate")) as kube:
            HOST.set_role("operator")
            K.send("PATCH", DEPLOYMENTS.format(ns="lab") + "/frigate", {"spec": {"replicas": 2}},
                   ctype="application/merge-patch+json")
            self.assertEqual([("PATCH", DEPLOYMENTS.format(ns="lab") + "/frigate")], self.written(kube, "PATCH"))
            self.assertEqual(2, kube.object(DEPLOYMENTS.format(ns="lab") + "/frigate")["spec"]["replicas"])

    def test_an_admin_and_homestead_itself_can_change_a_host_privileged_workload(self):
        for role in ("admin", None):
            with self.subTest(role=role), Kube(deployment("router", hostNetwork=True)) as kube:
                HOST.set_role(role)
                K.send("PATCH", DEPLOYMENTS.format(ns="lab") + "/router", {"spec": {"replicas": 0}},
                       ctype="application/merge-patch+json")
                self.assertEqual(1, len(self.written(kube, "PATCH")))

    def test_a_job_started_by_an_operator_is_checked_as_that_operator(self):
        with Kube(deployment("router", hostNetwork=True)) as kube:
            with HOST.acting_as("operator"), self.assertRaises(PermissionError):
                K.send("DELETE", DEPLOYMENTS.format(ns="lab") + "/router")
            self.assertEqual([], kube.writes)

    def test_writes_that_are_not_to_workloads_pass_this_fence(self):
        with Kube() as kube:
            HOST.set_role("operator")
            K.send("POST", CONFIGMAPS, {"apiVersion": "v1", "kind": "ConfigMap",
                                        "metadata": {"name": "notes", "namespace": "lab"}, "data": {"a": "1"}})
            self.assertEqual(1, len(self.written(kube, "POST")))

    def test_the_self_data_fence_stops_a_write_while_homesteads_data_is_moved(self):
        held = []

        @contextlib.contextmanager
        def activity():
            held.append("held")
            try:
                yield
            finally:
                held.append("released")

        def moving():
            raise RuntimeError("Homestead's data is being moved")

        with Kube() as kube, mock.patch.object(K, "self_data_activity", activity), \
                mock.patch.object(K, "require_self_data_write", moving):
            with self.assertRaisesRegex(RuntimeError, "being moved"):
                K.send("POST", CONFIGMAPS, {"apiVersion": "v1", "kind": "ConfigMap",
                                            "metadata": {"name": "notes", "namespace": "lab"}})
            self.assertEqual([], kube.writes)
        self.assertEqual(["held", "released"], held, "the fence is let go after a refusal")

    def test_the_self_data_fence_holds_while_the_write_runs(self):
        seen = []

        @contextlib.contextmanager
        def activity():
            seen.append("held")
            yield
            seen.append("released")

        with Kube() as kube, mock.patch.object(K, "self_data_activity", activity):
            kube.react("POST", CONFIGMAPS, lambda *a, **k: seen.append("written") or None)
            K.send("POST", CONFIGMAPS, {"apiVersion": "v1", "kind": "ConfigMap",
                                        "metadata": {"name": "notes", "namespace": "lab"}})
        self.assertEqual(["held", "written", "released"], seen)


if __name__ == "__main__":
    unittest.main()
