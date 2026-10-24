"""Run the installer's storage functions with disposable paths and host tools."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "scripts/bootstrap-k3s.sh").read_text(encoding="utf-8")


@unittest.skipUnless(os.name == "posix", "runs the host's POSIX shell")
class InstallerStorageLayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.fstab = self.root / "etc/fstab"
        self.fstab.parent.mkdir()
        self.fstab.write_text("# system mounts\n")
        self.old = self.root / "var/lib/longhorn"
        self.new = self.root / "mnt/longhorn-os"
        self.log = self.root / "calls"
        self.log.touch()
        for name, body in {
            "findmnt": "echo /dev/test-vg/root",
            "lvs": 'case "$*" in *--noheadings*) echo "test-vg /dev/test-vg/root /dev/mapper/test--vg-root";; *) [ "$EXISTING_LV" = yes ];; esac',
            "vgs": 'case "$*" in *vg_free*) echo 180;; *) echo 300;; esac',
            "mountpoint": '[ "$2" = "$MOUNTED" ]',
            "blkid": "echo example-filesystem-uuid",
            "lvcreate": "exit 0",
            "mkfs.ext4": '[ "$FAIL_FORMAT" != yes ]',
            "udevadm": "exit 0",
            "chattr": "exit 0",
            "mount": '[ "$FAIL_MOUNT" != yes ]',
        }.items():
            path = self.bin / name
            path.write_text(f'#!/bin/sh\nprintf "%s %s\\n" "{name}" "$*" >> "$CALLS"\n{body}\n')
            path.chmod(0o755)

    def run_storage(self, mode="server", path="", size="auto", **env):
        functions = SOURCE[SOURCE.index("longhorn_layout() {"):SOURCE.index("# Longhorn's volumes reach")]
        # Every filesystem write goes under this test's temporary directory.
        for absolute in ("/var/lib/longhorn", "/mnt/longhorn-os", "/etc/fstab", "/dev/$vg"):
            functions = functions.replace(absolute, str(self.root) + absolute)
        selected = str(self.root) + path if path else ""
        script = ('set -eu\nfail() { echo "$*" >&2; exit 1; }\n' + functions +
                  '\nlonghorn_layout\nprintf "SELECTED=%s\\n" "$LONGHORN_DATA_PATH"\nhost_longhorn_volume\n')
        return subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=5,
                              env={**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                                   "CALLS": str(self.log), "MODE": mode, "TIEBREAKER": "0",
                                   "LONGHORN_VOLUME": size, "LONGHORN_DATA_PATH": selected, **env})

    def mutations(self):
        return [line for line in self.log.read_text().splitlines()
                if line.split()[0] in ("lvcreate", "mkfs.ext4", "mount", "chattr")]

    def test_new_cluster_mounts_only_replica_storage_and_leaves_runtime_on_os(self):
        result = self.run_storage()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(f" {self.new} ext4 ", self.fstab.read_text())
        self.assertNotIn(str(self.old), self.fstab.read_text())
        self.assertFalse(self.old.exists())
        self.assertIn(f"mount {self.new}", self.mutations())

    def test_join_requires_the_existing_clusters_path_before_any_allocation(self):
        for mode in ("agent", "join"):
            result = self.run_storage(mode=mode)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("--longhorn-data-path", result.stderr)
            self.assertEqual([], self.mutations())

    def test_worker_can_join_a_separate_layout_cluster(self):
        result = self.run_storage(mode="agent", path="/mnt/longhorn-os")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(f" {self.new} ext4 ", self.fstab.read_text())

    def test_server_can_join_a_legacy_layout_cluster_without_changing_it(self):
        result = self.run_storage(mode="join", path="/var/lib/longhorn")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(f" {self.old} ext4 ", self.fstab.read_text())

    def test_existing_runtime_and_replicas_are_never_formatted(self):
        self.old.mkdir(parents=True)
        data = self.old / "replicas"
        data.write_text("existing data")
        result = self.run_storage()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("existing data", data.read_text())
        self.assertEqual([], self.mutations())

    def test_separate_layout_rerun_preserves_runtime_and_data(self):
        self.old.mkdir(parents=True)
        (self.old / "metadata").write_text("runtime")
        self.new.mkdir(parents=True)
        (self.new / "replicas").write_text("data")
        result = self.run_storage(MOUNTED=str(self.new))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(f"SELECTED={self.new}", result.stdout)
        self.assertEqual([], self.mutations())

    def test_missing_existing_mount_is_not_replaced_even_when_allocation_is_disabled(self):
        self.fstab.write_text(f"UUID=existing {self.new} ext4 defaults 0 2\n")
        result = self.run_storage(size="none")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("not mounted", result.stderr)
        self.assertEqual([], self.mutations())

    def test_missing_data_mount_blocks_even_with_runtime_and_fallback_data_present(self):
        self.old.mkdir(parents=True)
        (self.old / "metadata").write_text("runtime")
        self.new.mkdir(parents=True)
        (self.new / "unexpected").write_text("retain this")
        self.fstab.write_text(f"UUID=existing {self.new} ext4 defaults 0 2\n")
        result = self.run_storage()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("not mounted", result.stderr)
        self.assertEqual("retain this", (self.new / "unexpected").read_text())
        self.assertEqual([], self.mutations())

    def test_existing_unmounted_lv_is_retained_instead_of_reformatted(self):
        result = self.run_storage(EXISTING_LV="yes")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("left intact", result.stderr)
        self.assertEqual([], self.mutations())

    def test_mount_failure_restores_fstab_and_stops_installation(self):
        result = self.run_storage(FAIL_MOUNT="yes")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("LV is retained", result.stderr)
        self.assertEqual("# system mounts\n", self.fstab.read_text())

    def test_format_failure_keeps_new_lv_and_never_mounts(self):
        result = self.run_storage(FAIL_FORMAT="yes")
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(any(line.startswith("mount ") for line in self.mutations()))

    def test_explicit_path_cannot_relocate_existing_storage(self):
        self.old.mkdir(parents=True)
        (self.old / "replicas").write_text("data")
        result = self.run_storage(path="/mnt/longhorn-os")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("preserved", result.stderr)
        self.assertEqual([], self.mutations())

    def test_existing_chart_or_installation_keeps_all_values_on_addons_and_reruns(self):
        section = SOURCE[SOURCE.index('LONGHORN_FOUND=""'):SOURCE.index('if [ "$KUBEVIRT" = 1 ]; then install_kubevirt; fi')]
        for mode in ("server", "addons"):
            for existing in ("crd", "chart", "none", "denied", "chart-denied"):
                with self.subTest(mode=mode, existing=existing):
                    manifest = self.root / "longhorn.yaml"
                    manifest.write_text("existing configuration")
                    script = ('set -eu\nsay() { :; }\napply() { echo APPLIED; }\nfail() { echo "$*" >&2; exit 1; }\n'
                              'kube() { [ "$EXISTING" = denied ] && return 1; case "$*" in '
                              '*crd*) [ "$EXISTING" = crd ] && echo existing;; '
                              '*helmchart*) [ "$EXISTING" = chart-denied ] && return 1; [ "$EXISTING" = chart ] && echo existing;; '
                              '*) exit 9;; esac; return 0; }\n' + section)
                    result = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=5,
                                            env={**os.environ, "LONGHORN": "1", "MODE": mode, "KUBECTL": "kube",
                                                 "EXISTING": existing, "MANIFESTS": str(self.root),
                                                 "LONGHORN_VERSION": "", "LONGHORN_DATA_PATH": "/mnt/longhorn-os"})
                    self.assertEqual(1 if "denied" in existing else 0, result.returncode, result.stderr)
                    if existing == "none":
                        self.assertIn("APPLIED", result.stdout)
                        self.assertIn("defaultDataPath: /mnt/longhorn-os", manifest.read_text())
                    else:
                        self.assertNotIn("APPLIED", result.stdout)
                        self.assertEqual("existing configuration", manifest.read_text())


if __name__ == "__main__":
    unittest.main()
