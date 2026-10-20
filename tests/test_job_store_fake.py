import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import homestead_operations as ops
from fakes.job_store import Store, damaged_store, put, store_writes


class PutTests(unittest.TestCase):
    def store(self, items):
        saved = []
        store = Store(read=lambda: [dict(i) for i in items], write=lambda rows: saved.append((store._lock._is_owned(), rows)))
        return store, saved

    def test_put_saves_the_items_given_under_the_lock(self):
        store, saved = self.store([{"id": "a"}])
        put(store, [{"id": "b"}])
        self.assertEqual([(True, [{"id": "b"}])], saved)

    def test_put_without_items_saves_the_jobs_as_stored(self):
        store, saved = self.store([{"id": "a"}])
        put(store)
        self.assertEqual([(True, [{"id": "a"}])], saved)

    def test_a_put_that_fails_releases_the_lock(self):
        store = Store(read=list, write=lambda rows: (_ for _ in ()).throw(OSError("lost storage")))
        with self.assertRaises(OSError):
            put(store, [])
        self.assertFalse(store._lock._is_owned())


class RealStoreTests(unittest.TestCase):
    """The helpers that stand in for patching the real store's _read and _write."""
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patch = mock.patch.object(ops, "DATA_DIR", directory.name)
        patch.start()
        self.addCleanup(patch.stop)
        self.job = {"id": "job-1", "kind": "test", "status": "running", "ref": {}}

    def test_store_writes_records_each_save_of_the_jobs(self):
        with store_writes(ops) as written:
            put(ops, [self.job])
        self.assertEqual([[self.job]], written)
        self.assertEqual([self.job], ops.stored())

    def test_store_writes_ignores_other_files(self):
        with store_writes(ops) as written:
            ops.SHARED.write_json(str(Path(ops.DATA_DIR) / "other.json"), {"a": 1})
        self.assertEqual([], written)

    def test_a_failing_store_write_raises_and_keeps_what_was_stored(self):
        put(ops, [self.job])
        with store_writes(ops, fail=OSError("full disk")), self.assertRaisesRegex(OSError, "full disk"):
            put(ops, [])
        self.assertEqual([self.job], ops.stored())

    def test_an_exception_class_as_fail_raises_too(self):
        with store_writes(ops, fail=OSError), self.assertRaises(OSError):
            put(ops, [self.job])
        self.assertEqual([], ops.stored())

    def test_a_store_write_can_fail_only_for_some_jobs(self):
        def fail(items):
            if any(i["status"] == "failed" for i in items):
                raise OSError("lost storage")
        with store_writes(ops, fail=fail) as written:
            put(ops, [self.job])
            with self.assertRaises(OSError):
                put(ops, [{**self.job, "status": "failed"}])
        self.assertEqual([[self.job]], written)
        self.assertEqual([self.job], ops.stored())

    def test_a_damaged_store_cannot_be_read_and_is_put_back_after(self):
        put(ops, [self.job])
        with damaged_store(ops):
            with self.assertRaises(ValueError):
                ops.stored()
        self.assertEqual([self.job], ops.stored())

    def test_a_damaged_store_that_did_not_exist_is_removed_after(self):
        with damaged_store(ops):
            with self.assertRaises(ValueError):
                ops.stored()
        self.assertEqual([], ops.stored())


if __name__ == "__main__":
    unittest.main()
