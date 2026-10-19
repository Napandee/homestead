"""The job store is used through two public calls, stored() and
transaction(), not its private lock, read and write (#622 step 1)."""
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import homestead_operations as OPS

JOB = {"id": "op-1", "kind": "deployment", "status": "running", "ref": {}, "title": "Deploy web",
       "resource": {}, "href": "", "message": "", "created": "2026-10-19T00:00:00Z", "updated": "2026-10-19T00:00:00Z"}


class StoreApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        OPS.bind(lambda path: {}, self.tmp.name, lambda *a, **k: None)
        self.addCleanup(self.tmp.cleanup)

    def stored(self):
        with open(os.path.join(self.tmp.name, OPS.STORE), encoding="utf-8") as handle:
            return json.load(handle)

    def test_stored_is_what_is_stored(self):
        with OPS._lock:
            OPS._write([dict(JOB)])
        self.assertEqual(["op-1"], [item["id"] for item in OPS.stored()])

    def test_stored_keeps_what_the_page_snapshot_leaves_out(self):
        # snapshot() is what the page shows; stored() is the record itself.
        with OPS._lock:
            OPS._write([dict(JOB, ref={"marker": "internal"})])
        self.assertEqual({"marker": "internal"}, OPS.stored()[0]["ref"])
        self.assertNotIn("ref", OPS.snapshot()[0])

    def test_a_transaction_writes_only_when_saved(self):
        with OPS._lock:
            OPS._write([dict(JOB)])
        with OPS.transaction() as t:
            t.items[0]["message"] = "changed but not saved"
        self.assertEqual("", self.stored()[0]["message"])
        with OPS.transaction() as t:
            t.items[0]["message"] = "saved"
            t.save()
        self.assertEqual("saved", self.stored()[0]["message"])

    def test_save_can_be_given_a_new_list(self):
        with OPS.transaction() as t:
            t.save([dict(JOB, id="op-2")])
        self.assertEqual(["op-2"], [item["id"] for item in self.stored()])

    def test_stored_inside_a_transaction_reads_the_file(self):
        with OPS._lock:
            OPS._write([dict(JOB)])
        with OPS.transaction() as t:
            self.assertEqual([item["id"] for item in t.items], [item["id"] for item in OPS.stored()])

    def test_a_block_that_raises_writes_nothing_and_lets_go(self):
        with OPS._lock:
            OPS._write([dict(JOB)])
        with self.assertRaises(RuntimeError), OPS.transaction() as t:
            t.items[0]["message"] = "half done"
            raise RuntimeError("stopped part-way")
        self.assertEqual("", self.stored()[0]["message"])
        done = []

        def other():
            with OPS.transaction():
                done.append(1)
        thread = threading.Thread(target=other)
        thread.start()
        thread.join(3)
        self.assertEqual([1], done, "the lock was released")

    def test_the_lock_is_held_for_the_whole_block(self):
        entered, waited = threading.Event(), []

        def other():
            entered.wait(5)
            with OPS.transaction():
                waited.append("in")
        thread = threading.Thread(target=other)
        thread.start()
        with OPS.transaction():
            entered.set()
            thread.join(0.3)
            self.assertEqual([], waited, "another transaction waits while this one is open")
        thread.join(5)
        self.assertEqual(["in"], waited)

    def test_a_store_that_cannot_be_read_raises(self):
        with OPS._lock:
            OPS._write([dict(JOB)])
        with open(os.path.join(self.tmp.name, OPS.STORE), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with self.assertRaises(ValueError):
            OPS.stored()
        with self.assertRaises(ValueError), OPS.transaction() as t:
            t.items


# Modules that still reach into the store; each PR of #622 step 1 takes some out.
STILL_PRIVATE = {
    "homestead_disk_v2.py", "homestead_self_data_execute.py", "homestead_self_data_prepare.py",
    "homestead_import_job.py", "homestead_storage_guard.py", "homestead_vm_mutation_job.py",
    "homestead_vm_power_recovery.py", "homestead_vm_mutation_recovery.py", "homestead_storage_recovery.py",
    "homestead_reclass.py", "homestead_power_jobs.py", "homestead_lhv2_upgrade.py", "homestead_lhv2_setup.py",
}
PRIVATE = re.compile(r"\b[A-Za-z_]*(?:ops|OPS|operations|OPERATIONS)[A-Za-z_]*\.(?:_lock|_read|_write)\b")


class GuardTests(unittest.TestCase):
    def users(self):
        return {path.name for path in (ROOT / "server").glob("*.py")
                if path.name != "homestead_operations.py" and PRIVATE.search(path.read_text(encoding="utf-8"))}

    def test_only_the_listed_modules_still_use_the_private_store(self):
        self.assertEqual(set(), self.users() - STILL_PRIVATE,
                         "use OPS.stored() or OPS.transaction(), not the store's private lock, read or write")

    def test_the_list_is_exact(self):
        self.assertEqual(set(), STILL_PRIVATE - self.users(), "this module no longer uses them: take it off STILL_PRIVATE")


if __name__ == "__main__":
    unittest.main()
