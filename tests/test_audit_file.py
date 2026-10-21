"""The audit log on the data volume (#738): /data/audit.jsonl, 0600, two files
of 10 MB at most, and written only through the self-data write fence - while a
data move holds /data, lines go to stdout alone and nothing raises."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import homestead_audit as AUDIT_LOG
import homestead_self_data_fence as FENCE
import homestead_shared as SHARED
from homestead_storage_journal import Held


class OnTheDataVolume(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = folder.name
        self.path = os.path.join(self.dir, "audit.jsonl")
        self.enterContext(mock.patch.object(SHARED, "DIR", self.dir))
        self.enterContext(mock.patch.object(SHARED, "WRITE_GUARD", None))
        self.enterContext(mock.patch.object(SHARED, "WRITE_SCOPE", None))
        self.enterContext(mock.patch.object(AUDIT_LOG, "_file_on", return_value=True))
        self.stdout = self.enterContext(mock.patch.object(AUDIT_LOG, "_stdout"))

    def emit(self, n=1):
        for i in range(n):
            AUDIT_LOG.emit({"v": 1, "path": f"/api/x/{i}"})

    def kept(self):
        return Path(self.path).read_text(encoding="utf-8").splitlines() if os.path.exists(self.path) else []

    def test_lines_append_with_mode_0600(self):
        self.emit(2)
        self.assertEqual(["/api/x/0", "/api/x/1"], [json.loads(line)["path"] for line in self.kept()])
        self.assertEqual(0o600, os.stat(self.path).st_mode & 0o777)
        self.assertEqual(2, self.stdout.call_count, "stdout always")

    def test_rotation_keeps_exactly_two_files(self):
        with mock.patch.object(AUDIT_LOG, "MAX_BYTES", 200):
            self.emit(20)
        self.assertEqual(["audit.jsonl", "audit.jsonl.1", "audit.lock"], sorted(os.listdir(self.dir)))
        for name in ("audit.jsonl", "audit.jsonl.1"):
            self.assertLessEqual(os.path.getsize(os.path.join(self.dir, name)), 200)
        self.assertEqual("/api/x/19", json.loads(self.kept()[-1])["path"])

    def test_rotation_rechecks_under_the_lock(self):
        """Two replicas that both saw a full file rotate it once, not twice."""
        Path(self.path).write_text("x" * 300, encoding="utf-8")
        with mock.patch.object(AUDIT_LOG, "MAX_BYTES", 200):
            AUDIT_LOG._rotate(self.path, 10)
            Path(self.path).write_text("y\n", encoding="utf-8")       # the first replica's next line
            AUDIT_LOG._rotate(self.path, 10)                           # the second, late
        self.assertEqual("x" * 300, Path(self.path + ".1").read_text(encoding="utf-8"))
        self.assertEqual("y\n", Path(self.path).read_text(encoding="utf-8"))

    def test_a_held_fence_means_stdout_only(self):
        Path(self.path).write_text("old\n", encoding="utf-8")
        for name, held in (("WRITE_SCOPE", mock.Mock(side_effect=Held("held"))),
                           ("WRITE_GUARD", mock.Mock(side_effect=Held("held")))):
            with self.subTest(name), mock.patch.object(SHARED, name, held):
                self.emit()
                held.assert_called_with(self.path)
        self.assertEqual(["old"], self.kept())
        self.assertEqual(["audit.jsonl"], os.listdir(self.dir))
        self.assertEqual(2, self.stdout.call_count)

    @unittest.skipUnless(FENCE.fcntl is not None, "Linux shared locking")
    def test_a_frozen_barrier_means_stdout_only_until_it_thaws(self):
        """A data move's freeze drains activity; an audit append is activity."""
        locks = tempfile.TemporaryDirectory()
        self.addCleanup(locks.cleanup)
        mine, mover = FENCE.WriteBarrier(locks.name, lambda: None), FENCE.WriteBarrier(locks.name, lambda: None)
        with mock.patch.object(SHARED, "WRITE_SCOPE", lambda path: mine.activity()):
            with mover.freeze(timeout=.05):
                self.emit()
                self.assertEqual([], os.listdir(self.dir))
            self.emit()
        self.assertEqual(1, len(self.kept()), "after the freeze it writes again")
        self.assertEqual(2, self.stdout.call_count)

    def test_the_fence_sees_the_audit_path(self):
        guard = mock.Mock()
        with mock.patch.object(SHARED, "WRITE_GUARD", guard):
            self.emit()
        guard.assert_called_once_with(self.path)

    def test_no_data_dir_no_file(self):
        missing = os.path.join(self.dir, "missing")
        with mock.patch.object(SHARED, "DIR", missing):
            self.emit()
        self.assertFalse(os.path.exists(missing))
        self.assertEqual(1, self.stdout.call_count)

    def test_a_full_disk_changes_nothing(self):
        with mock.patch.object(AUDIT_LOG.os, "write", side_effect=OSError(28, "No space left on device")):
            self.emit()
        self.assertEqual(1, self.stdout.call_count)

    def test_a_link_in_its_place_is_not_followed(self):
        target = os.path.join(self.dir, "elsewhere")
        Path(target).write_text("", encoding="utf-8")
        os.symlink(target, self.path)
        self.emit()
        self.assertEqual("", Path(target).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
