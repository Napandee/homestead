"""Homestead's own permissions, kept to exactly what its release describes."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import homestead_kube as K
import homestead_self as SELF
import render_rbac
from fakes.kube import Kube

RBAC = "/apis/rbac.authorization.k8s.io/v1"


def installed(rules=None, subjects=("homestead",), account="homestead"):
    return [
        {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "homestead-abc-1", "namespace": "lab"},
         "spec": {"serviceAccountName": account}},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
         "metadata": {"name": "homestead", "resourceVersion": "7"},
         "rules": SELF.desired_rules() if rules is None else rules},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
         "metadata": {"name": "homestead"}, "roleRef": {"kind": "ClusterRole", "name": "homestead"},
         "subjects": [{"kind": "ServiceAccount", "name": s, "namespace": "lab"} for s in subjects]},
    ]


class SelfTest(unittest.TestCase):
    def use(self, objects, forbid=()):
        """Homestead's own objects on the shared fake API (tests/fakes/kube.py);
        forbid lists (method, path) writes the API server refuses with a 403."""
        self.kube = self.enterContext(Kube(*objects))
        for method, path in forbid:
            self.kube.fail(path, 403, times=99, method=method)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.addCleanup(SELF.bind, *(getattr(SELF, name, None) for name in ("kget", "ksend", "NS", "VERSION", "DATA_DIR")))
        SELF.bind(K.kget, K.ksend, "lab", "2.8.73", self.dir.name)
        patcher = mock.patch.object(SELF, "POD", "homestead-abc-1")
        patcher.start()
        self.addCleanup(patcher.stop)
        return self.kube

    def writes(self, method=None):
        return [(m, p) for m, p, _, _ in self.kube.writes if method in (None, m)]

    def role(self):
        return self.kube.object(f"{RBAC}/clusterroles/homestead")["rules"]


class ManifestTests(unittest.TestCase):
    def test_homestead_may_edit_its_own_role_and_no_other(self):
        for rule in SELF.desired_rules():
            if "rbac.authorization.k8s.io" in (rule.get("apiGroups") or []):
                self.assertEqual(["homestead"], rule.get("resourceNames"), rule)
            self.assertFalse({"bind", "impersonate", "*"} & set(rule.get("verbs") or []), rule)

    def test_the_permissions_file_is_the_manifests_own(self):
        self.assertEqual(render_rbac.render(), (ROOT / "deploy" / "rbac.yaml").read_text(encoding="utf-8"),
                         "run python scripts/render_rbac.py")
        rbac = (ROOT / "deploy" / "rbac.yaml").read_text(encoding="utf-8")
        for absent in ("kind: Deployment", "kind: Service\n", "kind: PersistentVolumeClaim", "kind: Namespace"):
            self.assertNotIn(absent, rbac)


class PermissionTests(SelfTest):
    def test_a_role_that_matches_is_left_alone(self):
        self.use(installed())
        self.assertEqual("current", SELF.reconcile()["state"])
        self.assertEqual([], self.writes("PUT"))

    def test_a_role_behind_the_release_gains_what_it_needs(self):
        self.use(installed(rules=SELF.desired_rules()[:-2]))
        result = SELF.reconcile()

        self.assertEqual("updated", result["state"])
        self.assertIn("added", result["detail"])
        self.assertEqual(SELF.desired_rules(), self.role())
        put = [body for method, path, body, _ in self.kube.writes if (method, path) == ("PUT", f"{RBAC}/clusterroles/homestead")]
        self.assertEqual(["7"], [body["metadata"].get("resourceVersion") for body in put],
                         "the update names the version it read, so a change made meanwhile is not lost")

    def test_a_permission_a_release_dropped_is_taken_away(self):
        retired = {"apiGroups": [""], "resources": ["serviceaccounts"], "verbs": ["delete"], "resourceNames": ["old"]}
        self.use(installed(rules=SELF.desired_rules() + [retired]))
        result = SELF.reconcile()

        self.assertEqual("updated", result["state"])
        self.assertIn("removed 1 permission", result["detail"])
        self.assertNotIn(retired, self.role())

    def test_the_binding_names_homestead_alone(self):
        self.use(installed(subjects=("homestead", "old-account")))
        SELF.reconcile()
        subjects = self.kube.object(f"{RBAC}/clusterrolebindings/homestead")["subjects"]
        self.assertEqual([{"kind": "ServiceAccount", "name": "homestead", "namespace": "lab"}], subjects)

    def test_without_the_one_time_command_settings_shows_it(self):
        self.use(installed(rules=SELF.desired_rules()[:-2]), forbid={("PUT", f"{RBAC}/clusterroles/homestead")})
        result = SELF.reconcile()

        self.assertEqual("manual", result["state"])
        self.assertEqual("kubectl apply -f https://raw.githubusercontent.com/homestead-lab/homestead/v2.8.73/deploy/rbac.yaml",
                         result["command"])

    def test_an_account_the_binding_does_not_name_is_told_what_to_run(self):
        self.use(installed(account="someone-else"))
        self.assertEqual("manual", SELF.reconcile()["state"])

    def test_names_restrict_and_wildcards_widen(self):
        live = [{"apiGroups": ["*"], "resources": ["*"], "verbs": ["*"]}]
        self.assertEqual([], SELF.missing(live, SELF.desired_rules()))
        narrow = [{"apiGroups": [""], "resources": ["serviceaccounts"], "verbs": ["delete"], "resourceNames": ["x"]}]
        self.assertEqual([("", "serviceaccounts", "delete", None)],
                         SELF.missing(narrow, [{"apiGroups": [""], "resources": ["serviceaccounts"], "verbs": ["delete"]}]))


class OldKeyTests(SelfTest):
    def cluster_with_old_keys(self):
        return [
            {"apiVersion": "apps/v1", "kind": "Deployment",
                "metadata": {"name": "frigate", "namespace": "lab",
                             "annotations": {"harvui.io/icon": "/api/icons/abc.png", "harvui.io/edited": "1",
                                             "homestead.io/edited": "2", "other": "kept"},
                             "labels": {"harvui.io/managed": "true", "app": "frigate"}},
                "spec": {"template": {"metadata": {"annotations": {"harvui.io/restarted": "t"}}}}},
            {"apiVersion": "v1", "kind": "Secret",
                "metadata": {"name": "homestead-src-unraid", "namespace": "lab",
                             "labels": {"harvui.io/managed": "true"}}, "data": {"k": "dg=="}},
            {"apiVersion": "v1", "kind": "Node",
             "metadata": {"name": "node1", "annotations": {"harvui.io/auto-hardware": "igpu"}}},
            {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "plain", "namespace": "lab"}},
        ]

    def test_old_keys_move_to_the_homestead_domain_on_metadata_only(self):
        self.use(self.cluster_with_old_keys())
        result = SELF.adopt_old_keys()
        objects = self.kube.object

        self.assertEqual(3, result["changed"])
        frigate = objects("/apis/apps/v1/namespaces/lab/deployments/frigate")
        # The shared fake runs the real write guards, which may mark who made the write; that
        # mark is the change history's, not part of moving the old keys.
        annotations = {key: value for key, value in frigate["metadata"]["annotations"].items()
                       if key != "homestead.io/changedBy"}
        self.assertEqual({"homestead.io/icon": "/api/icons/abc.png", "homestead.io/edited": "2", "other": "kept"},
                         annotations, "a newer value under the new key is kept")
        self.assertEqual({"homestead.io/managed": "true", "app": "frigate"}, frigate["metadata"]["labels"])
        self.assertEqual({"harvui.io/restarted": "t"}, frigate["spec"]["template"]["metadata"]["annotations"],
                         "a pod template is never touched, so nothing restarts")
        self.assertEqual({"homestead.io/auto-hardware": "igpu"}, objects("/api/v1/nodes/node1")["metadata"]["annotations"])
        secret = objects("/api/v1/namespaces/lab/secrets/homestead-src-unraid")
        self.assertEqual({"homestead.io/managed": "true"}, secret["metadata"]["labels"])
        self.assertEqual({"k": "dg=="}, secret["data"])
        self.assertIn(("PUT", "/api/v1/namespaces/lab/secrets/homestead-src-unraid"),
                      self.writes(), "Secrets are updated, not patched")

    def test_it_runs_once(self):
        self.use(self.cluster_with_old_keys())
        SELF.adopt_old_keys()
        writes = len(self.kube.writes)
        self.assertEqual({"done": True, "changed": 0}, SELF.adopt_old_keys())
        self.assertEqual(writes, len(self.kube.writes))


if __name__ == "__main__":
    unittest.main()
