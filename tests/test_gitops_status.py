"""#742: whether Flux is applying what it should - each Kustomization and
HelmRelease, the apps each applies, one summary, and an alert when one has
not been Ready for ten minutes. Read only."""
import io
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_gitops as GITOPS
import homestead_kube as KUBE
import homestead_routes as ROUTER

SHA = "246b3c65387098c5b0875586fc607cdbc5157a13"


def ready(status, message, at="2026-10-22T08:56:31Z"):
    return {"type": "Ready", "status": status, "reason": "x", "message": message, "lastTransitionTime": at}


KUSTOMIZATIONS = [
    {"metadata": {"namespace": "flux-system", "name": "apps"}, "spec": {"interval": "10m"},
     "status": {"conditions": [ready("True", f"Applied revision: main@sha1:{SHA}")],
                "lastAppliedRevision": f"main@sha1:{SHA}", "lastAttemptedRevision": f"main@sha1:{SHA}",
                "history": [{"lastReconciled": "2026-10-22T09:06:31Z"}]}},
    {"metadata": {"namespace": "flux-system", "name": "infra"}, "spec": {"interval": "10m"},
     "status": {"conditions": [ready("False", "kustomize build failed: missing secret example-key")],
                "lastAppliedRevision": f"main@sha1:{'a' * 40}", "history": []}},
    {"metadata": {"namespace": "flux-system", "name": "paused"}, "spec": {"suspend": True},
     "status": {"conditions": [ready("False", "suspended")]}},
]
HELMRELEASES = [
    {"metadata": {"namespace": "dev", "name": "runner"}, "spec": {"interval": "30m"},
     "status": {"conditions": [ready("True", "Helm upgrade succeeded")], "lastAttemptedRevision": "0.15.0",
                "history": [{"chartName": "runner-chart", "chartVersion": "0.15.0", "lastDeployed": "2026-10-08T09:00:02Z"}]}},
]
DEPLOYMENTS = [
    {"metadata": {"namespace": "lab", "name": "web", "labels": {"kustomize.toolkit.fluxcd.io/name": "apps",
                                                                 "kustomize.toolkit.fluxcd.io/namespace": "flux-system"}}},
    {"metadata": {"namespace": "dev", "name": "runner", "labels": {"helm.toolkit.fluxcd.io/name": "runner",
                                                                    "helm.toolkit.fluxcd.io/namespace": "dev"}}},
    {"metadata": {"namespace": "lab", "name": "plain", "labels": {}}},
]


def not_found(path):
    return urllib.error.HTTPError(path, 404, "not found", {}, io.BytesIO())


class FluxStatus(unittest.TestCase):
    def read(self, path, **_):
        self.paths.append(path)
        if path == "/apis":
            return {"groups": [{"name": "apps"}] + [{"name": g} for g in self.groups]}
        if path.startswith("/apis/kustomize.toolkit.fluxcd.io/v1/"):
            if "kustomize.toolkit.fluxcd.io" not in self.groups:
                raise not_found(path)
            return {"items": KUSTOMIZATIONS, "metadata": {}}
        if path.startswith("/apis/helm.toolkit.fluxcd.io/v2/"):
            if "helm.toolkit.fluxcd.io" not in self.groups:
                raise not_found(path)
            return {"items": HELMRELEASES, "metadata": {}}
        if path == "/apis/apps/v1/deployments":
            return {"items": DEPLOYMENTS, "metadata": {}}
        raise AssertionError(path)

    def setUp(self):
        self.paths, self.groups = [], ["kustomize.toolkit.fluxcd.io", "helm.toolkit.fluxcd.io"]
        for patch in (mock.patch.object(KUBE, "kget", side_effect=self.read),
                      mock.patch.object(ROUTER, "_cached", [lambda key, seconds, fn: fn()])):
            patch.start()
            self.addCleanup(patch.stop)

    def rows(self):
        return {row["id"]: row for row in GITOPS.status()["objects"]}

    def test_a_ready_kustomization_says_what_it_applied(self):
        row = self.rows()["Kustomization flux-system/apps"]
        self.assertEqual({"kind": "Kustomization", "namespace": "flux-system", "name": "apps", "ready": True,
                          "suspended": False, "revision": "main@246b3c6", "tried": "2026-10-22T09:06:31Z",
                          "since": "2026-10-22T08:56:31Z", "interval": "10m",
                          "message": f"Applied revision: main@sha1:{SHA}", "id": "Kustomization flux-system/apps"}, row)

    def test_a_helmrelease_gives_its_chart_and_version(self):
        row = self.rows()["HelmRelease dev/runner"]
        self.assertTrue(row["ready"])
        self.assertEqual("runner-chart 0.15.0", row["revision"])
        self.assertEqual("2026-10-08T09:00:02Z", row["tried"])

    def test_a_failing_one_keeps_flux_message(self):
        row = self.rows()["Kustomization flux-system/infra"]
        self.assertIs(False, row["ready"])
        self.assertIn("missing secret", row["message"])

    def test_apps_are_mapped_to_what_applies_them(self):
        self.assertEqual({"lab/web": "Kustomization flux-system/apps", "dev/runner": "HelmRelease dev/runner"},
                         GITOPS.status()["apps"])

    def test_summary_counts_and_names_what_is_not_ready(self):
        self.assertEqual("3 Kustomizations, 1 HelmRelease: 1 not ready, 1 suspended", GITOPS.status()["summary"])

    def test_flux_not_installed_is_nothing_to_show(self):
        self.groups = []
        self.assertEqual({"objects": [], "apps": {}, "summary": ""}, GITOPS.status())
        self.assertEqual([], GITOPS.alert_facts())

    def test_a_failed_read_is_an_error_not_an_empty_list(self):
        def broken(path, **_):
            raise urllib.error.HTTPError(path, 500, "boom", {}, io.BytesIO())
        with mock.patch.object(KUBE, "kget", side_effect=broken):
            with self.assertRaises(urllib.error.HTTPError):
                GITOPS.alert_facts()

    def test_alert_for_not_ready_after_ten_minutes_never_for_suspended(self):
        facts = GITOPS.alert_facts()
        self.assertEqual(["gitops:Kustomization:flux-system/infra"], [f["key"] for f in facts])
        fact = facts[0]
        self.assertEqual(("degraded", "degraded", 600), (fact["category"], fact["severity"], fact["hold"]))
        self.assertEqual("Flux has not applied Kustomization flux-system/infra", fact["title"])
        # Flux's message can name repositories, paths and Secrets: it stays on
        # the Cluster page, out of alerts that go to every device.
        self.assertNotIn("missing secret", fact["body"] + fact["title"])
        self.assertIn("Cluster", fact["body"])
        self.assertEqual("/system/cluster", fact["href"])

    def test_read_through_one_shared_minute(self):
        seen = []
        with mock.patch.object(ROUTER, "_cached", [lambda key, seconds, fn: seen.append((key, seconds)) or fn()]):
            GITOPS.status()
        self.assertEqual([("gitops", 60)], seen)

    def test_the_route_is_for_anyone_signed_in(self):
        self.assertEqual("viewer", GITOPS.ROUTES[("GET", "/api/gitops")][0])
        self.assertIn("homestead_gitops", ROUTER.MODULES)


    def test_reading_writes_nothing(self):
        """Read only (pinning: it passed before this test was added)."""
        with mock.patch.object(KUBE, "ksend") as send:
            GITOPS.status()
            GITOPS.alert_facts()
            GITOPS.ROUTES[("GET", "/api/gitops")][1](None)
        send.assert_not_called()

    def test_each_deployment_is_asked_once_who_applies_it(self):
        import homestead_updates as UPDATES
        with mock.patch.object(UPDATES, "managed_by", wraps=UPDATES.managed_by) as asked:
            GITOPS.status()
        self.assertEqual(len(DEPLOYMENTS), asked.call_count)

    def test_the_alerts_loop_reads_it(self):
        source = (Path(__file__).resolve().parents[1] / "server" / "homestead_overview_server.py").read_text()
        self.assertIn('take("gitops", GITOPS.alert_facts)', source)

if __name__ == "__main__":
    unittest.main()
