"""#742 part 2: Reconcile now. An admin asks Flux to apply an app again, the
way `flux reconcile` does: the reconcile.fluxcd.io/requestedAt annotation on
the Kustomization or HelmRelease that applies it, and nothing else."""
import copy
import io
import re
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "scripts"))
import homestead_audit as AUDIT
import homestead_gitops as GITOPS
import homestead_kube as KUBE
import homestead_route_policy as ROUTE_POLICY
import homestead_routes as ROUTER

KS = "/apis/kustomize.toolkit.fluxcd.io"
HR = "/apis/helm.toolkit.fluxcd.io"
ANNOTATION = "reconcile.fluxcd.io/requestedAt"


def flux_labels(kind, ns, name):
    prefix = "kustomize" if kind == "Kustomization" else "helm"
    return {f"{prefix}.toolkit.fluxcd.io/name": name, f"{prefix}.toolkit.fluxcd.io/namespace": ns}


OBJECTS = {
    f"{KS}/v1/namespaces/flux-system/kustomizations/apps": {
        "metadata": {"namespace": "flux-system", "name": "apps", "annotations": {ANNOTATION: "2026-10-26T08:00:00Z"}},
        "spec": {"interval": "10m"},
        "status": {"conditions": [{"type": "Ready", "status": "True"}], "lastHandledReconcileAt": "2026-10-26T08:00:00Z"}},
    f"{KS}/v1/namespaces/flux-system/kustomizations/paused": {
        "metadata": {"namespace": "flux-system", "name": "paused"}, "spec": {"suspend": True}, "status": {}},
    f"{HR}/v2/namespaces/dev/helmreleases/runner": {
        "metadata": {"namespace": "dev", "name": "runner"}, "spec": {"interval": "30m"},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
}
DEPLOYMENTS = [
    {"metadata": {"namespace": "lab", "name": "web", "labels": flux_labels("Kustomization", "flux-system", "apps")}},
    {"metadata": {"namespace": "lab", "name": "held", "labels": flux_labels("Kustomization", "flux-system", "paused")}},
    {"metadata": {"namespace": "dev", "name": "runner", "labels": flux_labels("HelmRelease", "dev", "runner")}},
    {"metadata": {"namespace": "lab", "name": "plain", "labels": {}}},
]


def not_found(path):
    return urllib.error.HTTPError(path, 404, "not found", {}, io.BytesIO())


class ReconcileNow(unittest.TestCase):
    def read(self, path, **_):
        self.reads.append(path)
        path = self.served(path)
        if path == "/apis":
            return {"groups": [{"name": "apps"}, {"name": "kustomize.toolkit.fluxcd.io"}, {"name": "helm.toolkit.fluxcd.io"}]}
        if path == "/apis/apps/v1/deployments":
            return {"items": DEPLOYMENTS, "metadata": {}}
        for base, plural in ((f"{KS}/v1", "kustomizations"), (f"{HR}/v2", "helmreleases")):
            if path == f"{base}/{plural}":
                return {"items": [copy.deepcopy(o) for p, o in self.objects.items() if p.startswith(base + "/")],
                        "metadata": {}}
        if path in self.objects:
            return copy.deepcopy(self.objects[path])
        raise not_found(path)

    def served(self, path):
        """An older Flux serves Kustomizations as v1beta2 only."""
        if self.old_flux and path.startswith(f"{KS}/"):
            if path.startswith(f"{KS}/v1/"):
                raise not_found(path)
            path = path.replace(f"{KS}/v1beta2/", f"{KS}/v1/")
        return path

    def setUp(self):
        self.reads, self.sent, self.old_flux = [], [], False
        self.objects = copy.deepcopy(OBJECTS)
        GITOPS._following[0] = 0
        for patch in (mock.patch.object(KUBE, "kget", side_effect=self.read),
                      mock.patch.object(KUBE, "ksend", side_effect=lambda *a, **k: self.sent.append((a, k)) or {}),
                      mock.patch.object(ROUTER, "_cached", [lambda key, seconds, fn: fn()])):
            patch.start()
            self.addCleanup(patch.stop)

    def reconcile(self, body):
        request = ROUTER.Request("POST", "/api/gitops/reconcile", {}, body, "ada", "admin", "192.0.2.10")
        return GITOPS.ROUTES[("POST", "/api/gitops/reconcile")][1](request)

    def only_patch(self):
        self.assertEqual(1, len(self.sent), self.sent)
        (method, path, body), options = self.sent[0][0][:3], self.sent[0][1]
        ctype = options.get("ctype") or (self.sent[0][0][3] if len(self.sent[0][0]) > 3 else None)
        return method, path, body, ctype

    def test_it_sets_requested_at_on_the_kustomization_that_applies_the_app(self):
        answer = self.reconcile({"ns": "lab", "name": "web"})
        method, path, body, ctype = self.only_patch()
        self.assertEqual(("PATCH", f"{KS}/v1/namespaces/flux-system/kustomizations/apps"), (method, path))
        self.assertEqual("application/merge-patch+json", ctype)
        self.assertEqual({"metadata": {"annotations": {ANNOTATION: answer["requested"]}}}, body)
        self.assertRegex(answer["requested"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$")
        self.assertEqual("Kustomization flux-system/apps", answer["id"])

    def test_a_helm_release_is_patched_where_it_lives(self):
        self.reconcile({"ns": "dev", "name": "runner"})
        method, path, body, _ = self.only_patch()
        self.assertEqual(("PATCH", f"{HR}/v2/namespaces/dev/helmreleases/runner"), (method, path))
        self.assertEqual([ANNOTATION], list(body["metadata"]["annotations"]))

    def test_an_older_flux_is_patched_at_the_version_it_serves(self):
        self.old_flux = True
        self.reconcile({"ns": "lab", "name": "web"})
        self.assertEqual(f"{KS}/v1beta2/namespaces/flux-system/kustomizations/apps", self.only_patch()[1])

    def test_refused_for_an_app_flux_does_not_apply(self):
        with self.assertRaisesRegex(ValueError, "Flux does not apply lab/plain"):
            self.reconcile({"ns": "lab", "name": "plain"})
        with self.assertRaisesRegex(ValueError, "Flux does not apply lab/nothing"):
            self.reconcile({"ns": "lab", "name": "nothing"})
        self.assertEqual([], self.sent)

    def test_refused_for_a_suspended_one(self):
        with self.assertRaisesRegex(ValueError, "suspended"):
            self.reconcile({"ns": "lab", "name": "held"})
        self.assertEqual([], self.sent)

    def test_refused_without_an_app(self):
        for body in ({}, {"ns": "lab"}, {"ns": "lab", "name": ""}, {"ns": ["lab"], "name": "web"}, None):
            with self.assertRaises(ValueError):
                self.reconcile(body)
        self.assertEqual([], self.sent)

    def test_only_an_admin_may_ask(self):
        self.assertEqual("admin", GITOPS.ROUTES[("POST", "/api/gitops/reconcile")][0])
        self.assertEqual("admin", ROUTE_POLICY.role("/api/gitops/reconcile", "POST"))
        self.assertIn("homestead_gitops", ROUTER.MODULES)

    def test_each_row_says_when_a_reconcile_was_asked_and_handled(self):
        rows = {row["id"]: row for row in GITOPS.status()["objects"]}
        self.assertEqual(("2026-10-26T08:00:00Z", "2026-10-26T08:00:00Z"),
                         (rows["Kustomization flux-system/apps"]["requested"], rows["Kustomization flux-system/apps"]["handled"]))
        self.assertEqual(("", ""), (rows["HelmRelease dev/runner"]["requested"], rows["HelmRelease dev/runner"]["handled"]))

    def test_after_asking_the_status_is_read_again_soon(self):
        seen, forgotten = [], []
        with mock.patch.object(ROUTER, "forget", side_effect=lambda *keys: forgotten.extend(keys)):
            self.reconcile({"ns": "lab", "name": "web"})
        self.assertIn("gitops", forgotten)
        with mock.patch.object(ROUTER, "_cached", [lambda key, seconds, fn: seen.append(seconds) or fn()]):
            GITOPS.status()
            GITOPS._following[0] = 0
            GITOPS.status()
        self.assertEqual([GITOPS.FOLLOW_EVERY, GITOPS.EVERY], seen)

    def test_the_change_audit_names_the_app(self):
        request = ROUTER.Request("POST", "/api/gitops/reconcile", {}, {"ns": "lab", "name": "web"}, "ada", "admin", "192.0.2.10")
        entry = AUDIT.line(request, 200, 12, {})
        self.assertEqual(("POST", "/api/gitops/reconcile", {"ns": "lab", "name": "web"}),
                         (entry["method"], entry["path"], entry["target"]))
        self.assertNotIn(("POST", "/api/gitops/reconcile"), AUDIT.NOT_AUDITED)


class ReconcileRbac(unittest.TestCase):
    """Homestead may patch Kustomizations and HelmReleases, and the rule that
    says so names nothing else, in deploy.yaml, rbac.yaml and the chart."""

    def rule(self, text):
        match = re.search(r"(?m)^  - apiGroups: \[\"kustomize\.toolkit\.fluxcd\.io\", \"helm\.toolkit\.fluxcd\.io\"\]\n"
                          r"    resources: \[kustomizations, helmreleases\]\n    verbs: \[patch\]$", text)
        return match

    def test_deploy_yaml_grants_patch_on_the_two_flux_kinds_only(self):
        self.assertIsNotNone(self.rule((ROOT / "deploy" / "deploy.yaml").read_text()))

    def test_rbac_yaml_is_rendered_with_it(self):
        import render_rbac
        rendered = (ROOT / "deploy" / "rbac.yaml").read_text()
        self.assertEqual(render_rbac.render(), rendered)
        self.assertIsNotNone(self.rule(rendered))

    def test_the_chart_carries_it(self):
        chart = (ROOT / "charts" / "homestead" / "templates" / "rbac.yaml").read_text()
        self.assertTrue("kustomize.toolkit.fluxcd.io" in chart, "the chart has no Flux rule")
        self.assertRegex(chart, r"resources: \[kustomizations, helmreleases\]\n\s+verbs: \[patch\]")


if __name__ == "__main__":
    unittest.main()
