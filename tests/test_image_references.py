"""The image cache lists every image a node holds, and never calls one unused
while a stopped container or a scheduled job still starts from it."""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_imports as imports

ESPHOME = "sha256:" + "e" * 64
BACKUP = "sha256:" + "f" * 64


def ready(name, images):
    return {"metadata": {"name": name},
            "status": {"conditions": [{"type": "Ready", "status": "True"}], "images": images}}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = imports.SCAN_DIR, dict(imports._SCANS), imports._SCAN_STARTED[0]
        imports.SCAN_DIR = self.tmp.name
        imports._SCANS.clear()
        imports._SCAN_STARTED[0] = 0.0
        self.sent = []
        self.nodes = [ready("node1", [
            {"names": ["ghcr.io/esphome/esphome:2025.9.0", "ghcr.io/esphome/esphome@" + ESPHOME], "sizeBytes": 600 << 20},
            {"names": ["docker.io/restic/restic:0.17", "docker.io/restic/restic@" + BACKUP], "sizeBytes": 30 << 20},
            {"names": ["docker.io/library/unused:1"], "sizeBytes": 10 << 20}])]
        self.objects = {
            "/api/v1/pods": {"items": []},
            "/apis/apps/v1/deployments": {"items": [
                {"metadata": {"name": "esphome", "namespace": "lab"},
                 "spec": {"replicas": 0, "template": {"spec": {"containers": [
                     {"name": "esphome", "image": "ghcr.io/esphome/esphome:2025.9.0"}]}}}},
                {"metadata": {"name": "web", "namespace": "lab"},
                 "spec": {"replicas": 1, "template": {"spec": {"containers": [
                     {"name": "web", "image": "docker.io/library/unused:1"}]}}}}]},
            "/apis/apps/v1/statefulsets": {"items": []},
            "/apis/batch/v1/cronjobs": {"items": [
                {"metadata": {"name": "backup", "namespace": "lab"},
                 "spec": {"jobTemplate": {"spec": {"template": {"spec": {"containers": [
                     {"name": "restic", "image": "restic/restic:0.17"}]}}}}}}]},
        }

        def get(path):
            if path == "/api/v1/nodes":
                return {"items": self.nodes}
            if "/pods?" in path or "/daemonsets?" in path:
                return {"items": []}
            return self.objects[path]

        def send(method, path, body=None, **kwargs):
            self.sent.append((method, path, body))
            return body or {}
        self.restore = imports.kget, imports.ksend
        imports.kget, imports.ksend = get, send
        imports.NAMES.bind(get)

    def tearDown(self):
        imports.kget, imports.ksend = self.restore
        imports.SCAN_DIR = self.saved[0]
        imports._SCANS.clear(); imports._SCANS.update(self.saved[1])
        imports._SCAN_STARTED[0] = self.saved[2]
        self.tmp.cleanup()

    def image(self, report, name):
        return next(row for row in report["images"] if name in row["name"])


class StoppedReferenceTests(Fixture):
    def test_a_stopped_containers_image_is_kept_and_says_why(self):
        esphome = self.image(imports.image_cache(), "esphome")
        self.assertTrue(esphome["protected"])
        self.assertEqual([("stopped", "esphome")], [(r["reason"], r["workload"]) for r in esphome["retained_by"]])

    def test_it_cannot_be_cleaned_up_while_the_container_is_there(self):
        with self.assertRaisesRegex(PermissionError, "stopped:lab/esphome"):
            imports.cleanup_image(ESPHOME, ["node1"])

    def test_a_scheduled_jobs_image_is_kept_between_runs(self):
        restic = self.image(imports.image_cache(), "restic")
        self.assertEqual(["scheduled"], [r["reason"] for r in restic["retained_by"]])

    def test_a_running_workload_is_named_by_its_pods_not_as_stopped(self):
        self.assertFalse(self.image(imports.image_cache(), "unused")["protected"])

    def test_the_digest_it_last_ran_on_holds_the_image_whatever_its_tag(self):
        dep = self.objects["/apis/apps/v1/deployments"]["items"][0]
        dep["spec"]["template"]["spec"]["containers"][0]["image"] = "ghcr.io/esphome/esphome:stable"
        dep["metadata"]["annotations"] = {"homestead.io/ran-digests": '{"esphome": "%s"}' % ESPHOME}
        self.assertTrue(self.image(imports.image_cache(), "esphome")["protected"])


class ScanTests(Fixture):
    LISTING = [{"repoTags": ["ghcr.io/esphome/esphome:2025.9.0"], "repoDigests": ["ghcr.io/esphome/esphome@" + ESPHOME],
                "size": str(600 << 20)},
               {"repoTags": ["docker.io/library/small:1"], "repoDigests": [], "size": "1024"}]

    def scanned(self, at):
        imports._SCANS["node1"] = {"at": at, "images": self.LISTING}
        imports._save_scans()

    def test_a_scan_is_seen_by_another_replica_and_after_a_restart(self):
        self.scanned(time.time())
        imports._SCANS.clear()
        report = imports.image_cache()
        self.assertTrue(report["complete"])
        self.assertIn("docker.io/library/small:1", {row["name"] for row in report["images"]})

    def test_an_old_scan_is_still_used_with_what_kubernetes_lists_added(self):
        self.scanned(time.time() - imports.SCAN_FRESH - 60)
        names = {row["name"] for row in imports.image_cache()["images"]}
        # small:1 only the scan knows; restic only Kubernetes lists (pulled since).
        self.assertTrue({"docker.io/library/small:1", "docker.io/restic/restic:0.17"} <= names)

    def test_an_old_scan_is_asked_again_but_not_over_and_over(self):
        self.scanned(time.time() - imports.SCAN_FRESH - 60)
        with mock.patch.object(imports.RUNTIME, "pod", lambda *a, **k: {"spec": {}}):
            first = imports.image_cache()
            again = imports.image_cache()
        posts = [path for method, path, _ in self.sent if method == "POST" and path.endswith("/pods")]
        self.assertEqual(["node1"], first["scanning"])
        self.assertEqual(1, len(posts))
        self.assertEqual([], again["scanning"])

    def test_a_cleaned_image_leaves_the_saved_scan(self):
        self.scanned(time.time())
        dep = self.objects["/apis/apps/v1/deployments"]["items"][0]
        dep["spec"]["replicas"] = 1           # running, and with no pod here: nothing holds it
        with mock.patch.object(imports.RUNTIME, "pod", lambda *a, **k: {"spec": {}}):
            imports.cleanup_image(ESPHOME, ["node1"])
        imports._SCANS.clear()
        imports._load_scans()
        self.assertEqual([["docker.io/library/small:1"]], [i["repoTags"] for i in imports._SCANS["node1"]["images"]])


class UnreadInventoryTests(Fixture):
    """Every source of "still needed" is read in full, or nothing is removed (#620)."""
    UNUSED = "sha256:" + "a" * 64
    LISTS = ("/api/v1/pods", "/apis/apps/v1/deployments", "/apis/apps/v1/statefulsets", "/apis/batch/v1/cronjobs")

    def setUp(self):
        super().setUp()
        self.nodes[0]["status"]["images"][2]["names"].append("docker.io/library/unused@" + self.UNUSED)
        # A removal pod needs the runtime's paths, which come from the platform;
        # since #662 an unread platform is an error, not a guess, so give it one.
        platform = mock.patch("homestead_platform.detect", lambda *_: {"distribution": "k3s"})
        platform.start()
        self.addCleanup(platform.stop)

    def posted(self):
        """Removal pods started (a scan pod may start too; it removes nothing)."""
        return [body["metadata"]["name"] for method, path, body in self.sent if method == "POST"
                and body["metadata"]["name"].startswith("homestead-image-clean-")]

    def test_an_image_nothing_needs_is_cleaned_up(self):
        imports.cleanup_image(self.UNUSED, ["node1"])
        self.assertEqual(1, len(self.posted()))

    def test_nothing_is_removed_when_a_list_cannot_be_read(self):
        read = imports.kget
        for failed in self.LISTS:
            for how, answer in (("timed out", OSError("timed out")),
                                ("cut short", {"items": [], "metadata": {"continue": "next"}})):
                def get(path, failed=failed, answer=answer):
                    if path == failed:
                        if isinstance(answer, Exception):
                            raise answer
                        return answer
                    return read(path)
                with self.subTest(failed=failed, how=how), mock.patch.object(imports, "kget", get):
                    with self.assertRaisesRegex(ValueError, "no image is cleaned up"):
                        imports.cleanup_image(self.UNUSED, ["node1"])
                    self.assertEqual([], self.posted())


if __name__ == "__main__":
    unittest.main()
