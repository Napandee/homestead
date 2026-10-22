"""The image cache lists every image a node holds, and never calls one unused
while a stopped container or a scheduled job still starts from it."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import urllib.error
import homestead_imports as imports

VMS = "/apis/kubevirt.io/v1/virtualmachines"
POOLS = "/apis/pool.kubevirt.io/v1alpha1/virtualmachinepools"
VMIRS = "/apis/kubevirt.io/v1/virtualmachineinstancereplicasets"
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
            {"names": ["docker.io/library/unused:1"], "sizeBytes": 10 << 20},
            {"names": ["quay.io/containerdisks/fedora:41"], "sizeBytes": 500 << 20},
            {"names": ["quay.io/containerdisks/debian:12"], "sizeBytes": 400 << 20}])]
        self.objects = {
            "/api/v1/pods": {"items": []},
            POOLS: {"items": []},
            VMIRS: {"items": []},
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
            VMS: {"items": [
                {"metadata": {"name": "lab-vm", "namespace": "lab"},
                 "spec": {"runStrategy": "Halted", "template": {"spec": {"volumes": [
                     {"name": "rootdisk", "containerDisk": {"image": "quay.io/containerdisks/fedora:41"}},
                     {"name": "cloudinit", "cloudInitNoCloud": {"userData": "#cloud-config"}}]}}}},
                {"metadata": {"name": "web-vm", "namespace": "lab"},
                 "spec": {"runStrategy": "Always", "template": {"spec": {"volumes": [
                     {"name": "rootdisk", "containerDisk": {"image": "quay.io/containerdisks/debian:12"}}]}}},
                 "status": {"printableStatus": "Running", "ready": True}}]},
        }

        self.asked = []

        def get(path):
            self.asked.append(path)
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

    def test_a_stopped_vms_container_disk_is_kept_and_says_why(self):
        fedora = self.image(imports.image_cache(), "fedora")
        self.assertTrue(fedora["protected"])
        self.assertEqual([("stopped", "lab", "lab-vm", "rootdisk")],
                         [(r["reason"], r["namespace"], r["workload"], r["container"]) for r in fedora["retained_by"]])

    def test_a_stopped_vms_kernel_boot_container_is_kept(self):
        vm = self.objects[VMS]["items"][0]
        vm["spec"]["template"]["spec"]["domain"] = {"firmware": {"kernelBoot": {"container": {
            "image": "docker.io/library/unused:1", "kernelPath": "/boot/vmlinuz"}}}}
        unused = self.image(imports.image_cache(), "unused")
        self.assertTrue(unused["protected"])
        self.assertIn(("stopped", "lab-vm", "kernel-boot"),
                      [(r["reason"], r["workload"], r["container"]) for r in unused["retained_by"]])

    def test_a_running_vm_is_named_by_its_pod_not_as_stopped(self):
        self.assertFalse(self.image(imports.image_cache(), "debian")["protected"])

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
            imports.image_cache()
            self.assertEqual([], [p for m, p, _ in self.sent if m == "POST"], "looking starts nothing (#622)")
            imports.tick()
            imports.tick()
        posts = [path for method, path, _ in self.sent if method == "POST" and path.endswith("/pods")]
        self.assertEqual(1, len(posts), "the leader's tick asks once, not every minute")

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
    LISTS = ("/api/v1/pods", "/apis/apps/v1/deployments", "/apis/apps/v1/statefulsets", "/apis/batch/v1/cronjobs",
             VMS, POOLS, VMIRS)

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

    def kubevirt_answers_404(self, served):
        """The VM list answers 404; discovery answers with these groups, or
        raises when given an exception."""
        read = imports.kget

        def get(path):
            if path == VMS:
                raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
            if path == "/apis":
                if isinstance(served, Exception):
                    raise served
                return {"groups": [{"name": name} for name in served]}
            return read(path)
        return mock.patch.object(imports, "kget", get)

    # Today's behaviour, pinned before the unreadable-state changes: what can
    # be read still decides alone, and only an unreadable record keeps more.
    def test_a_rollback_annotation_keeps_only_the_image_it_names(self):
        web = self.objects["/apis/apps/v1/deployments"]["items"][1]
        web["metadata"]["annotations"] = {"homestead.io/update-previous": json.dumps(
            {"images": {"web": "docker.io/library/unused@" + self.UNUSED}, "at": "2026-10-01T00:00:00Z"})}
        report = imports.image_cache()
        self.assertEqual([("rollback", "web")],
                         [(r["reason"], r["workload"]) for r in self.image(report, "unused")["retained_by"]])
        self.assertFalse(self.image(report, "debian")["protected"])

    def test_a_rollback_annotation_naming_no_images_keeps_nothing(self):
        web = self.objects["/apis/apps/v1/deployments"]["items"][1]
        web["metadata"]["annotations"] = {"homestead.io/update-previous": json.dumps({"images": {}})}
        imports.cleanup_image(self.UNUSED, ["node1"])
        self.assertEqual(1, len(self.posted()))

    def test_an_unreadable_ran_digests_note_still_keeps_the_image_by_name(self):
        dep = self.objects["/apis/apps/v1/deployments"]["items"][0]
        dep["metadata"]["annotations"] = {"homestead.io/ran-digests": "not json"}
        self.assertTrue(self.image(imports.image_cache(), "esphome")["protected"])

    # Gap 11: a record Homestead cannot read keeps every image until it can,
    # and says whose it is, rather than reading as "this keeps nothing".
    def assert_everything_kept_by(self, workload, what):
        report = imports.image_cache()
        for name in ("unused", "debian"):
            rows = self.image(report, name)["retained_by"]
            self.assertIn(("unreadable", "lab", workload, what),
                          [(r["reason"], r["namespace"], r["workload"], r["container"]) for r in rows])
        with self.assertRaisesRegex(PermissionError, f"unreadable:lab/{workload}"):
            imports.cleanup_image(self.UNUSED, ["node1"])
        self.assertEqual([], self.posted())
        import homestead_image_cleanup as cleanup
        self.assertEqual([], cleanup.unused(report))

    def test_an_unreadable_rollback_annotation_keeps_every_image(self):
        web = self.objects["/apis/apps/v1/deployments"]["items"][1]
        for value in ("{not json", "[]", '"text"', json.dumps({"images": ["nginx"]}), json.dumps({"images": []}),
                      json.dumps({"images": None}), json.dumps({"images": ""}),
                      json.dumps({"images": {"web": 7}})):
            with self.subTest(value=value):
                web["metadata"]["annotations"] = {"homestead.io/update-previous": value}
                self.assert_everything_kept_by("web", "update-previous")

    def test_a_stopped_workload_with_an_unreadable_template_keeps_every_image(self):
        for kind, path, index, broken in (
                ("Deployment", "/apis/apps/v1/deployments", 0, lambda o: o["spec"].pop("template")),
                ("Deployment", "/apis/apps/v1/deployments", 0, lambda o: o["spec"]["template"]["spec"].update(containers="x")),
                ("Deployment", "/apis/apps/v1/deployments", 0, lambda o: o["spec"]["template"]["spec"].update(containers=["x"])),
                ("CronJob", "/apis/batch/v1/cronjobs", 0, lambda o: o["spec"]["jobTemplate"].update(spec=None)),
                ("VM", VMS, 0, lambda o: o["spec"].update(template="x")),
                ("VM", VMS, 0, lambda o: o["spec"]["template"]["spec"].update(volumes={"rootdisk": {}}))):
            with self.subTest(kind=kind):
                obj = self.objects[path]["items"][index]
                saved = json.loads(json.dumps(obj))
                broken(obj)
                try:
                    self.assert_everything_kept_by(obj["metadata"]["name"], "template")
                finally:
                    self.objects[path]["items"][index] = saved

    # Gap 13: a VM pool or VM replica set scaled to zero still starts from its
    # template's container disks; with no VM or pod to name them they were unused.
    def test_a_vm_pool_or_replica_set_at_zero_keeps_its_container_disks(self):
        disk = {"name": "rootdisk", "containerDisk": {"image": "docker.io/library/unused:1"}}
        for path, item in (
                (POOLS, {"metadata": {"name": "workers", "namespace": "lab"}, "spec": {"replicas": 0,
                 "virtualMachineTemplate": {"spec": {"template": {"spec": {"volumes": [disk]}}}}}}),
                (VMIRS, {"metadata": {"name": "workers", "namespace": "lab"}, "spec": {"replicas": 0,
                 "template": {"spec": {"volumes": [disk]}}}})):
            with self.subTest(path=path):
                self.objects[path] = {"items": [item]}
                try:
                    rows = self.image(imports.image_cache(), "unused")["retained_by"]
                    self.assertEqual([("stopped", "lab", "workers", "rootdisk")],
                                     [(r["reason"], r["namespace"], r["workload"], r["container"]) for r in rows])
                    with self.assertRaisesRegex(PermissionError, "stopped:lab/workers"):
                        imports.cleanup_image(self.UNUSED, ["node1"])
                    self.assertEqual([], self.posted())
                    item["spec"]["replicas"] = 2       # running ones are named by their VMs and pods
                    self.asked.clear()
                    self.assertFalse(self.image(imports.image_cache(), "unused")["protected"])
                    self.assertIn(path, self.asked)
                finally:
                    self.objects[path] = {"items": []}

    def test_no_permission_to_list_vm_pools_or_replica_sets_keeps_every_image(self):
        # An older Homestead role may not let it list these; the page still
        # opens, every image is kept, and the row says which list and why.
        read = imports.kget
        for path, kind in ((POOLS, "VirtualMachinePools"), (VMIRS, "VirtualMachineInstanceReplicaSets")):
            def get(asked, path=path):
                if asked == path:
                    raise urllib.error.HTTPError(asked, 403, "Forbidden", {}, None)
                return read(asked)
            with self.subTest(path=path), mock.patch.object(imports, "kget", get):
                report = imports.image_cache()
                for name in ("unused", "debian"):
                    self.assertIn(("unreadable", "", kind, "no permission to list"),
                                  [(r["reason"], r["namespace"], r["workload"], r["container"])
                                   for r in self.image(report, name)["retained_by"]])
                with self.assertRaisesRegex(PermissionError, f"unreadable:/{kind}"):
                    imports.cleanup_image(self.UNUSED, ["node1"])
                self.assertEqual([], self.posted())

    def test_other_errors_on_those_lists_still_stop_the_page(self):
        read = imports.kget

        def get(asked):
            if asked == POOLS:
                raise urllib.error.HTTPError(asked, 500, "Server Error", {}, None)
            return read(asked)
        with mock.patch.object(imports, "kget", get):
            with self.assertRaisesRegex(ValueError, "no image is cleaned up"):
                imports.image_cache()

    def test_replicas_count_as_zero_unless_a_positive_number(self):
        for replicas, zero in ((0, True), (2, False), (-1, True), ("2", True), ("x", True),
                               (True, True), (2.0, True)):
            with self.subTest(replicas=replicas):
                self.assertIs(zero, imports._scaled_to_zero({"replicas": replicas}))
        self.assertFalse(imports._scaled_to_zero({}), "missing means one, the Kubernetes default")
        self.assertFalse(imports._scaled_to_zero({"replicas": None}))

    def test_without_vm_pools_installed_clean_up_still_works(self):
        read = imports.kget

        asked = []

        def get(path):
            asked.append(path)
            if path == POOLS:
                raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
            if path == "/apis":
                return {"groups": [{"name": "apps"}, {"name": "kubevirt.io"}]}
            return read(path)
        with mock.patch.object(imports, "kget", get):
            imports.cleanup_image(self.UNUSED, ["node1"])
        self.assertIn(POOLS, asked, "the pool list is asked for, and its 404 then checked with discovery")
        self.assertIn("/apis", asked)
        self.assertEqual(1, len(self.posted()))

    def test_a_404_for_vm_pools_while_their_group_is_served_removes_nothing(self):
        read = imports.kget

        def get(path):
            if path == POOLS:
                raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
            if path == "/apis":
                return {"groups": [{"name": "apps"}, {"name": "kubevirt.io"}, {"name": "pool.kubevirt.io"}]}
            return read(path)
        with mock.patch.object(imports, "kget", get):
            with self.assertRaisesRegex(ValueError, "no image is cleaned up"):
                imports.cleanup_image(self.UNUSED, ["node1"])
        self.assertEqual([], self.posted())

    def test_without_kubevirt_installed_clean_up_still_works(self):
        with self.kubevirt_answers_404(["apps", "batch"]):
            imports.cleanup_image(self.UNUSED, ["node1"])
        self.assertEqual(1, len(self.posted()))

    def test_a_404_for_vms_while_kubevirt_is_served_removes_nothing(self):
        with self.kubevirt_answers_404(["apps", "batch", "kubevirt.io"]):
            with self.assertRaisesRegex(ValueError, "no image is cleaned up"):
                imports.cleanup_image(self.UNUSED, ["node1"])
        self.assertEqual([], self.posted())

    def test_an_empty_discovery_answer_removes_nothing(self):
        with self.kubevirt_answers_404([]):
            with self.assertRaisesRegex(ValueError, "no image is cleaned up"):
                imports.cleanup_image(self.UNUSED, ["node1"])
        self.assertEqual([], self.posted())

    def test_a_discovery_that_fails_after_a_404_removes_nothing(self):
        with self.kubevirt_answers_404(OSError("timed out")):
            with self.assertRaisesRegex(ValueError, "no image is cleaned up"):
                imports.cleanup_image(self.UNUSED, ["node1"])
        self.assertEqual([], self.posted())


if __name__ == "__main__":
    unittest.main()
