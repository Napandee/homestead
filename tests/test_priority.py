"""Priority tiers: the classes Homestead keeps, reading an app's tier, the
reviewed change for many apps, setting one in Edit, and the headroom a
critical app's reservation gets."""
import copy
import io
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_lifecycle as lifecycle
import homestead_priority as priority
import homestead_usage as usage
import server


def deployment(name, ns="lab", image="busybox", cls="", labels=None):
    spec = {"containers": [{"name": "app", "image": image}]}
    if cls:
        spec["priorityClassName"] = cls
    return {"metadata": {"name": name, "namespace": ns, "labels": labels or {}, "generation": 1},
            "spec": {"replicas": 1, "template": {"spec": spec}},
            "status": {"observedGeneration": 1, "replicas": 1, "updatedReplicas": 1, "readyReplicas": 1}}


def not_found(path):
    return urllib.error.HTTPError(path, 404, "not found", {}, io.BytesIO())


def managed(dep):
    if (dep["metadata"].get("labels") or {}).get("kustomize.toolkit.fluxcd.io/name"):
        return {"by": "Flux", "source": "Kustomization flux-system/apps", "detected": True}
    return {}


class PriorityTests(unittest.TestCase):
    def setUp(self):
        self.deps = {
            "lab/home-assistant": deployment("home-assistant", image="ghcr.io/home-assistant/home-assistant:stable"),
            "lab/vault": deployment("vault", image="vaultwarden/server", cls="homestead-critical"),
            "lab/paperless": deployment("paperless"),
            "ci/actions-runner": deployment("actions-runner", ns="ci"),
            "lab/unifi": deployment("unifi", cls="unifi-high"),
            "media/jellyfin": deployment("jellyfin", ns="media", labels={"kustomize.toolkit.fluxcd.io/name": "apps"}),
            "homestead/homestead": deployment("homestead", ns="homestead"),
        }
        self.classes, self.sent = {}, []
        priority.bind(self.kget, self.ksend, managed, ("homestead", "homestead"), {"kube-system"}, lambda: True)

    def kget(self, path):
        if path == "/apis/apps/v1/deployments":
            return {"items": [copy.deepcopy(d) for d in self.deps.values()]}
        if "/priorityclasses/" in path:
            name = path.rsplit("/", 1)[1]
            if name not in self.classes:
                raise not_found(path)
            return self.classes[name]
        ns, name = path.split("/namespaces/")[1].split("/deployments/")
        if f"{ns}/{name}" not in self.deps:
            raise not_found(path)
        return copy.deepcopy(self.deps[f"{ns}/{name}"])

    def ksend(self, method, path, body=None, ctype="application/json"):
        self.sent.append((method, path, body, ctype))
        if method == "POST":
            self.classes[body["metadata"]["name"]] = body

    def test_no_class_is_normal_and_a_foreign_class_is_other(self):
        self.assertEqual(("normal", ""), priority.tier_of({}))
        self.assertEqual(("critical", "homestead-critical"), priority.tier_of({"priorityClassName": "homestead-critical"}))
        self.assertEqual(("other", "unifi-high"), priority.tier_of({"priorityClassName": "unifi-high"}))

    def test_the_classes_are_made_once_with_low_never_evicting_and_none_the_default(self):
        priority.ensure()
        priority.ensure()
        made = {body["metadata"]["name"]: body for method, _, body, _ in self.sent if method == "POST"}
        self.assertEqual({"homestead-critical", "homestead-low"}, set(made))
        self.assertEqual(2, len([s for s in self.sent if s[0] == "POST"]))
        self.assertEqual("Never", made["homestead-low"]["preemptionPolicy"])
        self.assertTrue(made["homestead-critical"]["value"] > 0 > made["homestead-low"]["value"])
        self.assertFalse(any(b["globalDefault"] for b in made.values()))

    def test_only_the_class_a_tier_needs_is_made(self):
        priority.ensure(("low",))
        self.assertEqual(["homestead-low"], [b["metadata"]["name"] for m, _, b, _ in self.sent if m == "POST"])

    def test_permissions_from_before_this_version_say_so_plainly(self):
        denied = urllib.error.HTTPError("x", 403, "forbidden", {}, io.BytesIO())
        priority.bind(mock.Mock(side_effect=denied), self.ksend, managed, ("homestead", "homestead"), {"kube-system"}, lambda: True)
        with self.assertRaisesRegex(PermissionError, "permissions predate this version"):
            priority.ensure(("critical",))

    def test_a_class_made_meanwhile_by_another_save_is_taken_as_made(self):
        def ksend(method, path, body=None, ctype=None):
            raise urllib.error.HTTPError(path, 409, "exists", {}, io.BytesIO())
        priority.bind(self.kget, ksend, managed, ("homestead", "homestead"), {"kube-system"}, lambda: True)
        self.assertEqual([], priority.ensure(("critical",)))

    def test_the_job_fails_plainly_when_it_may_not_make_the_classes(self):
        plan = priority.plan()
        apps = priority.reviewed([a for a in plan["apps"] if a["tier"] != a["current"]], plan["review_token"])
        with mock.patch.object(priority, "ensure", side_effect=PermissionError(priority.NO_PERMISSION)):
            state, _, message = priority.status({"ref": {"apps": apps, "index": 0, "done": 0, "stage": "apply"}})
        self.assertEqual("failed", state)
        self.assertIn("permissions", message)

    def test_an_exporter_or_backup_beside_a_database_is_not_suggested_critical(self):
        for name in ("postgres-exporter", "mariadb-backup", "adguardhome-sync"):
            self.assertEqual((None, ""), priority.suggestion(deployment(name, image=f"example/{name}")), name)
        self.assertEqual("critical", priority.suggestion(deployment("db", image="postgres:16"))[0])

    def test_an_operator_is_not_offered_apps_only_an_admin_may_change(self):
        def guard(dep, ns, role):
            if role == "operator" and dep["metadata"]["name"] == "paperless":
                raise PermissionError("only an admin can operate management, system, service-account or host-privileged workloads")
        with mock.patch.object(priority, "guard", guard):
            plan = priority.plan(None, "operator")
            self.assertNotIn("lab/paperless", {a["id"] for a in plan["apps"]})
            self.assertIn("only an admin", {s["id"]: s for s in plan["skipped"]}["lab/paperless"]["why"])

    def test_a_class_someone_made_with_another_value_is_kept_and_said(self):
        self.classes["homestead-critical"] = {"value": 5}
        notes = priority.ensure()
        self.assertIn("kept", notes[0])
        self.assertNotIn("homestead-critical", [b["metadata"]["name"] for m, _, b, _ in self.sent if m == "POST"])

    def test_likely_critical_and_low_apps_start_at_their_suggestion(self):
        apps = {a["id"]: a for a in priority.plan()["apps"]}
        self.assertEqual(("normal", "critical"), (apps["lab/home-assistant"]["current"], apps["lab/home-assistant"]["tier"]))
        self.assertEqual("low", apps["ci/actions-runner"]["tier"])
        self.assertEqual("normal", apps["lab/paperless"]["tier"])
        self.assertEqual("critical", apps["lab/vault"]["tier"])                     # already set: no change

    def test_homestead_flux_apps_and_foreign_classes_are_not_changed(self):
        plan = priority.plan()
        apps = {a["id"]: a for a in plan["apps"]}
        self.assertNotIn("homestead/homestead", apps)
        self.assertEqual("normal", apps["media/jellyfin"]["tier"])
        self.assertIn("set priorityClassName there", apps["media/jellyfin"]["note"])
        self.assertIn("unifi-high", {s["id"]: s for s in plan["skipped"]}["lab/unifi"]["why"])
        with self.assertRaisesRegex(ValueError, "not one Homestead changes"):
            priority.check("media", "jellyfin", "normal")

    def test_the_review_covers_exactly_the_changes_and_a_tier_set_since_is_kept(self):
        plan = priority.plan({"lab/paperless": "low"})
        changes = [a for a in plan["apps"] if a["tier"] != a["current"]]
        self.assertEqual({"lab/home-assistant", "ci/actions-runner", "lab/paperless"}, {c["id"] for c in changes})
        self.assertEqual(3, len(priority.reviewed(changes, plan["review_token"])))
        with self.assertRaisesRegex(ValueError, "differ from the review"):
            priority.reviewed(changes, priority.plan()["review_token"])
        self.deps["lab/paperless"]["spec"]["template"]["spec"]["priorityClassName"] = "homestead-critical"
        with self.assertRaisesRegex(ValueError, "changed since the review"):
            priority.reviewed(changes, plan["review_token"])

    def test_the_job_makes_the_classes_then_sets_each_app_and_normal_removes_the_class(self):
        self.deps["lab/vault"]["spec"]["template"]["spec"]["priorityClassName"] = "homestead-critical"
        plan = priority.plan({"lab/vault": "normal", "lab/home-assistant": "normal", "ci/actions-runner": "normal"})
        apps = priority.reviewed([a for a in plan["apps"] if a["tier"] != a["current"]], plan["review_token"])
        item = {"ref": {"apps": apps, "index": 0, "done": 0, "stage": "apply"}}
        self.assertIn("Priority classes ready", priority.status(item)[2])
        priority.status(item)
        patch = [s for s in self.sent if s[0] == "PATCH"][0]
        self.assertEqual("/apis/apps/v1/namespaces/lab/deployments/vault", patch[1])
        self.assertEqual({"spec": {"template": {"spec": {"priorityClassName": None, "priority": None}}}}, patch[2])
        self.assertEqual("application/strategic-merge-patch+json", patch[3])
        priority.status(item)
        self.assertEqual(("succeeded", 100), priority.status(item)[:2])

    def test_edit_sets_and_clears_a_tier_and_leaves_a_foreign_class(self):
        spec = {}
        lifecycle._apply_priority(spec, "critical")
        self.assertEqual("homestead-critical", spec["priorityClassName"])
        lifecycle._apply_priority(spec, "normal")
        self.assertNotIn("priorityClassName", spec)
        foreign = {"priorityClassName": "unifi-high"}
        lifecycle._apply_priority(foreign, "low")
        self.assertEqual("unifi-high", foreign["priorityClassName"])
        copied = {"priority": 0}
        lifecycle._apply_priority(copied, "low")
        self.assertEqual({"priorityClassName": "homestead-low"}, copied)
        with self.assertRaisesRegex(ValueError, "critical, normal or low"):
            lifecycle._apply_priority({}, "urgent")

    def test_the_edit_dialog_reads_the_tier_back(self):
        dep = deployment("vault", cls="homestead-critical")
        with mock.patch.object(server.LC, "seed_configs", return_value=[]):
            payload = server.workload_edit_payload("lab", "vault", dep, hardware_definitions=[], services=[])
        self.assertEqual(("critical", "homestead-critical"), (payload["priority"], payload["priority_class"]))


class HeadroomTests(unittest.TestCase):
    def test_a_critical_app_reserves_more_memory_than_a_normal_one(self):
        with tempfile.TemporaryDirectory() as folder:
            usage.bind(folder, server.parse_cpu, server.parse_mem)
            try:
                for i in range(usage.MIN_ROUNDS):
                    usage.record([("lab", "ha", "ha", 100, 400)], now=1_760_000_000 + i * usage.STEP)
                now = 1_760_000_000 + usage.MIN_ROUNDS * usage.STEP
                normal = int(usage.summary("lab", "ha", "ha", now=now)["suggest"]["memory"][:-2])
                critical = int(usage.summary("lab", "ha", "ha", now=now, tier="critical")["suggest"]["memory"][:-2])
            finally:
                usage.bind("/data", server.parse_cpu, server.parse_mem)
        self.assertGreater(critical, normal)
        self.assertTrue(592 <= critical <= 640, critical)          # 400Mi x 1.5, within half a bucket


if __name__ == "__main__":
    unittest.main()
