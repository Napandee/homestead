"""#734: a review that writes an app's Deployment says who applies the app -
Flux, Argo CD or an administrator's mark - so a change git will put back is
not made blind, and a rename of such an app needs its own acknowledgement."""
import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import homestead_capacity_review as CAPACITY_REVIEW
import homestead_gitops as GITOPS
import homestead_rename as RENAME
import homestead_routes as ROUTER
import homestead_workloads as W


def deployment(labels=None, annotations=None, name="web"):
    return {"metadata": {"name": name, "namespace": "lab", "uid": "u1", "resourceVersion": "7",
                         "labels": dict(labels or {}), "annotations": dict(annotations or {})},
            "spec": {"replicas": 1, "template": {"metadata": {"labels": {"app": name}},
                                                 "spec": {"containers": [{"name": "main", "image": "example/web:1"}]}}}}


FLUX = {"kustomize.toolkit.fluxcd.io/name": "apps", "kustomize.toolkit.fluxcd.io/namespace": "flux-system"}
FLUX_MANAGED = {"by": "Flux", "source": "Kustomization flux-system/apps", "detected": True}


class Request:
    def __init__(self, body):
        self.body = body


class ReviewsSayWhoAppliesTheApp(unittest.TestCase):
    def setUp(self):
        self.current = deployment(FLUX)
        for patch in (mock.patch.object(W, "kget", side_effect=lambda path: copy.deepcopy(self.current), create=True),
                      mock.patch.object(W, "guard_managed_smb", create=True),
                      mock.patch.object(W.CAPACITY_REVIEW, "issue", return_value="1.token")):
            patch.start()
            self.addCleanup(patch.stop)

    def edit_review(self, body=None):
        plan = {"blocked": False, "warnings": [], "requires_confirmation": True}
        with mock.patch.object(W, "edit_capacity_plan", return_value=({"deployment": self.current}, {}, plan)):
            return W.preview_edit(Request(body or {"ns": "lab", "name": "web"}))

    def move_review(self):
        plan = {"blocked": False, "warnings": [], "move": {"node": "b", "mode": "preferred", "target": None}}
        with mock.patch.object(W, "move_capacity_plan", return_value=(self.current, plan, {})):
            return W.preview_host_move({"ns": "lab", "name": "web", "node": "b"})

    def test_edit_review_names_flux(self):
        self.assertEqual(FLUX_MANAGED, self.edit_review()["managed"])

    def test_plain_app_review_has_empty_managed(self):
        self.current = deployment()
        self.assertEqual({}, self.edit_review()["managed"])
        self.assertEqual({}, self.move_review()["managed"])

    def test_move_review_names_flux(self):
        self.assertEqual(FLUX_MANAGED, self.move_review()["managed"])

    def test_marked_app_is_named_by_its_mark(self):
        self.current = deployment(annotations={"homestead.io/updates-managed": "Renovate"})
        self.assertEqual({"by": "Renovate", "source": "", "detected": False}, self.edit_review()["managed"])

    def test_edit_review_offers_the_patch(self):
        """#735: the edit as YAML for git - only what changed."""
        proposed = copy.deepcopy(self.current)
        proposed["spec"]["template"]["spec"]["containers"][0]["image"] = "example/web:2"
        plan = {"blocked": False, "warnings": [], "requires_confirmation": True}
        with mock.patch.object(W, "edit_capacity_plan", return_value=({"deployment": proposed}, {}, plan)):
            reply = W.preview_edit(Request({"ns": "lab", "name": "web"}))
        self.assertEqual("apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n  namespace: lab\n"
                         "spec:\n  template:\n    spec:\n      containers:\n      - name: main\n        image: example/web:2",
                         reply["yaml"])

    def test_the_patch_never_carries_a_secret_value(self):
        """Security: values the save keeps in the app's own Secret - masked
        fields, secret-looking names, and ones already there - appear in the
        YAML only as secretKeyRef, never as a value in any encoding."""
        import base64
        import urllib.parse
        secrets = {"DB_PASSWORD": "hunter2-old-pass", "WEBHOOK": "s3cr3t-hook-value", "API_TOKEN": "tok-123-new-value",
                   "KEPT": "kept-in-secret-value"}
        self.current["metadata"]["annotations"]["homestead.io/env-secret"] = "web-env"
        self.current["spec"]["template"]["spec"]["containers"][0]["env"] = [
            {"name": "DB_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "web-env", "key": "main.DB_PASSWORD"}}},
            {"name": "KEPT", "valueFrom": {"secretKeyRef": {"name": "web-env", "key": "main.KEPT"}}},
            {"name": "TZ", "value": "UTC"}]
        proposed = copy.deepcopy(self.current)
        # What prepare_edit hands over: the form's values as plain literals.
        proposed["spec"]["template"]["spec"]["containers"][0]["env"] = [
            {"name": name, "value": value} for name, value in {**secrets, "TZ": "Europe/Example"}.items()]
        plan = {"blocked": False, "warnings": [], "requires_confirmation": True}
        with mock.patch.object(W, "edit_capacity_plan", return_value=({"deployment": proposed}, {}, plan)):
            reply = W.preview_edit(Request({"ns": "lab", "name": "web", "masked_env": ["WEBHOOK"]}))
        text = reply["yaml"]
        for value in secrets.values():
            for form in (value, base64.b64encode(value.encode()).decode(), urllib.parse.quote(value),
                         base64.b64encode(value.encode()).decode().rstrip("=")):
                self.assertNotIn(form, text)
        self.assertIn("secretKeyRef", text)
        self.assertIn("key: main.WEBHOOK", text)
        self.assertIn("Europe/Example", text)

    def test_a_review_that_changes_nothing_to_commit_has_no_yaml(self):
        self.assertEqual("", self.edit_review()["yaml"])

    def test_rename_review_has_no_yaml(self):
        plan = {"blocked": False, "warnings": [], "rename": {"from": "web", "to": "web2"}}
        with mock.patch.object(W, "edit_capacity_plan", return_value=({"deployment": self.current}, {"action": RENAME.KIND}, plan)):
            self.assertEqual("", W.preview_edit(Request({"ns": "lab", "name": "web", "workload_name": "web2"}))["yaml"])

    def test_every_spec_review_returns_managed(self):
        """Guard: every review in the apps module is listed in GITOPS.REVIEWS
        and carries managed. A new review that writes a Deployment's spec
        must say who applies the app too."""
        previews = {key for key in W.ROUTES if key[1].endswith("/preview")}
        self.assertEqual(previews, set(GITOPS.REVIEWS))
        for method, path in GITOPS.REVIEWS:
            reply = self.edit_review() if path == "/api/edit/preview" else self.move_review()
            self.assertEqual(FLUX_MANAGED, reply.get("managed"), path)


class RenameOfAnAppGitApplies(unittest.TestCase):
    def setUp(self):
        self.current = deployment(FLUX)
        self.body = {"ns": "lab", "name": "web", "workload_name": "web2", "capacity_token": "1.x", "confirm_capacity": True}
        context = {"action": RENAME.KIND, "source": {}, "target": "web2"}
        for patch in (mock.patch.object(W, "kget", side_effect=lambda path: copy.deepcopy(self.current), create=True),
                      mock.patch.object(W, "guard_managed_smb", create=True),
                      mock.patch.object(W, "edit_capacity_plan", return_value=({"deployment": self.current}, context, {})),
                      mock.patch.object(W, "is_self", return_value=False, create=True),
                      mock.patch.object(W, "ksend", create=True),
                      mock.patch.object(W.CAPACITY_REVIEW, "enforce"),
                      mock.patch.object(ROUTER, "forget")):
            patch.start()
            self.addCleanup(patch.stop)
        self.dispatch = mock.patch.object(W.RENAME, "dispatch", return_value={"ok": True}).start()
        self.addCleanup(mock.patch.stopall)

    def test_rename_of_managed_app_needs_the_tick(self):
        with self.assertRaises(ValueError) as refused:
            W.edit(Request(dict(self.body)))
        self.assertIn("confirm_managed", str(refused.exception))
        self.assertIn("Flux", str(refused.exception))
        self.dispatch.assert_not_called()
        self.assertEqual({"ok": True}, W.edit(Request({**self.body, "confirm_managed": True})))
        self.dispatch.assert_called_once()

    def test_rename_of_plain_app_needs_no_tick(self):
        self.current = deployment()
        self.assertEqual({"ok": True}, W.edit(Request(dict(self.body))))

    def test_the_tick_is_an_acknowledgement_not_part_of_the_review(self):
        """Ticking after the review keeps the review's token valid, and the
        rename's own field check accepts it."""
        self.assertIn("confirm_managed", CAPACITY_REVIEW.CONTROL_FIELDS)
        with mock.patch.object(RENAME, "identity", return_value={"namespace": "lab", "name": "web"}), \
                mock.patch.object(RENAME, "guards"), mock.patch.object(RENAME.KUBE, "get_optional", return_value=None):
            proposed, _ = RENAME.prepare({**self.body, "confirm_managed": True}, deployment(FLUX),
                                         lambda dep, ns, name: {**dep, "metadata": {**dep["metadata"], "name": name}}, None)
        self.assertEqual("web2", proposed["metadata"]["name"])


if __name__ == "__main__":
    unittest.main()
