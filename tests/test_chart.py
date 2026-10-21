import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "server"))
import render_chart  # noqa: E402


class ChartTests(unittest.TestCase):
    """The chart is generated from the manifests, so it cannot drift from them."""

    def test_the_checked_in_chart_is_what_the_manifests_render(self):
        release = render_chart.version()
        expected = render_chart.files(release)
        chart = ROOT / "charts" / "homestead"
        on_disk = {str(p.relative_to(chart)).replace("\\", "/"): p.read_text(encoding="utf-8")
                   for p in chart.rglob("*") if p.is_file()}
        self.assertEqual(sorted(expected), sorted(on_disk), "run python scripts/render_chart.py")
        for name, text in expected.items():
            self.assertEqual(text, on_disk[name], f"{name} is stale: run python scripts/render_chart.py")

    def test_the_chart_is_the_release(self):
        chart = render_chart.files(render_chart.version())["Chart.yaml"]
        self.assertIn(f"version: {render_chart.version()}\n", chart)
        self.assertIn(f'appVersion: "{render_chart.version()}"', chart)

    def test_permissions_come_from_deploy_yaml_with_both_namespaces(self):
        rbac = render_chart.rbac()
        self.assertIn("kind: ClusterRole\n", rbac)
        self.assertIn("  namespace: {{ .Values.workloadNamespace.name }}", rbac)   # console role, where apps are
        self.assertIn("    namespace: {{ .Release.Namespace }}", rbac)             # the service account
        self.assertNotIn("namespace: lab", rbac)

    def test_the_setup_code_role_is_in_homesteads_own_namespace(self):
        """#737: reading, making and deleting the setup-code Secret is granted
        where Homestead runs, not where the apps go."""
        rbac = render_chart.rbac()
        role = next(doc for doc in rbac.split("\n---\n") if "kind: Role\n" in doc and "name: homestead-setup" in doc)
        self.assertIn("  namespace: {{ .Release.Namespace }}", role)
        self.assertIn("resources: [secrets]", role)
        self.assertIn("resourceNames: [homestead-setup]", role)
        self.assertIn("verbs: [get, delete]", role)
        self.assertNotIn("update", role.split("rules:", 1)[1])
        binding = next(doc for doc in rbac.split("\n---\n") if "kind: RoleBinding" in doc and "name: homestead-setup" in doc)
        self.assertIn("  namespace: {{ .Release.Namespace }}", binding)
        self.assertIn("homestead-setup", (ROOT / "deploy" / "rbac.yaml").read_text(encoding="utf-8"))

    def test_a_setup_code_can_be_set_ahead_and_the_notes_show_where_it_is(self):
        files = render_chart.files(render_chart.version())
        self.assertIn('setupCode: ""', files["values.yaml"])
        secret = files["templates/setup-code.yaml"]
        self.assertTrue(secret.startswith("{{- with .Values.setupCode }}"))
        self.assertIn("name: homestead-setup", secret)
        self.assertIn("namespace: {{ $.Release.Namespace }}", secret)
        self.assertIn("{{- fail", secret)
        self.assertIn("kubectl -n {{ .Release.Namespace }} get secret homestead-setup -o jsonpath='{.data.code}' | base64 -d",
                      files["templates/NOTES.txt"])
        self.assertNotIn("sets up the admin account. Apps", files["templates/NOTES.txt"])

    def test_the_probe_ships_its_scripts_and_follows_the_image(self):
        files = render_chart.files(render_chart.version())
        self.assertIn("files/probe/probe.py", files)
        daemonset = files["templates/nodeprobe.yaml"]
        self.assertIn('image: {{ include "homestead.image" . }}', daemonset)
        self.assertIn("checksum/scripts", daemonset)


if __name__ == "__main__":
    unittest.main()
