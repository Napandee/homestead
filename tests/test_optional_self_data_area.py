"""Optional reads and deletes in Homestead's own-data modules (#619 step 3):
each reads through the reader it was given, a missing object (404) is None
(or "already gone" for a delete), and any other failure raises or holds as
before. The 404 handling lives in homestead_kube, not in a copy per module."""
import os
import subprocess
import sys
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

SERVER = Path(os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as KUBE
import homestead_self as SELF
import homestead_self_data_bootstrap as BOOTSTRAP
import homestead_self_data_fence as FENCE
import homestead_self_data_finish as FINISH
import homestead_self_data_prepare as PREPARE
import homestead_storage_journal as JOURNAL
import homestead_transfer_cleanup as CLEANUP
from test_optional_vms_area import Paths, functions_comparing_404, http_error, never

# Functions that may still compare a status with 404 themselves, and why.
OWN_404 = {
    "homestead_self": set(),
    # Both sort a refused write into "refused" or "uncertain"; there 404 is
    # one of the refusals, not "not there".
    "homestead_self_data_bootstrap": {"step"},
    "homestead_storage_journal": {"write"},
    "homestead_self_data_finish": set(),
    "homestead_self_data_prepare": set(),
    "homestead_transfer_cleanup": set(),
    # _inspect's control-record read: a 404 there means "normal, writable",
    # and a null answer must keep failing rather than read as that.
    "homestead_self_data_fence": {"_inspect"},
    # A null answer for the handoff record must keep holding the move, not
    # read as "no record".
    "homestead_self_data_server": {"_require_no_data_handoff"},
}

# Modules that import each other (directly or through homestead_operations);
# each must load first on its own.
CYCLE = ("homestead_storage_journal", "homestead_kube", "homestead_self", "homestead_self_data_bootstrap",
         "homestead_operations", "homestead_self_data_fence", "homestead_transfer_cleanup")


class NoOwn404CopiesTests(unittest.TestCase):
    def test_only_the_listed_functions_compare_a_status_with_404(self):
        for module, allowed in OWN_404.items():
            with self.subTest(module=module):
                self.assertEqual(allowed, functions_comparing_404((SERVER / f"{module}.py").read_text()))


class ImportOrderTests(unittest.TestCase):
    def test_each_module_in_the_cycle_loads_first(self):
        for first in CYCLE:
            with self.subTest(first=first):
                done = subprocess.run([sys.executable, "-c", f"import {', '.join((first,) + CYCLE)}"], cwd=SERVER,
                                      capture_output=True, text=True, timeout=120)
                self.assertEqual(0, done.returncode, done.stderr[-600:])


class SharedClientUnused(unittest.TestCase):
    """The shared client fails if anything reaches it: every read and delete
    must go through the reader or sender the module was given."""
    def setUp(self):
        for name in ("kget", "ksend"):
            patch = mock.patch.object(KUBE, name, never)
            patch.start()
            self.addCleanup(patch.stop)


class SelfBindingTests(SharedClientUnused):
    BINDING = "/clusterrolebindings/"

    def bind(self, binding, sends=None):
        self.read = Paths(binding)
        self.sends = Paths({}) if sends is None else sends
        for name, value in (("kget", self.read), ("ksend", self.sends), ("NS", "homestead")):
            patch = mock.patch.object(SELF, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def binding(self, *names):
        return {"roleRef": {"kind": "ClusterRole", "name": "homestead-role"},
                "subjects": [{"kind": "ServiceAccount", "name": name, "namespace": "homestead"} for name in names]}

    def test_bound_role_reads_through_its_own_reader(self):
        self.bind(self.binding("homestead"))
        self.assertEqual("homestead-role", SELF.bound_role("homestead"))
        self.assertEqual(1, len(self.read.calls))
        self.assertIn(self.BINDING, self.read.calls[0][0])

    def test_bound_role_is_empty_when_the_binding_is_missing_or_unreadable(self):
        for answer in (http_error(404), http_error(403), http_error(500)):
            with self.subTest(code=answer.code):
                self.bind(answer)
                self.assertEqual("", SELF.bound_role("homestead"))

    def test_own_binding_writes_nothing_when_the_binding_is_missing_or_unreadable(self):
        for answer in (http_error(404), http_error(403), http_error(500)):
            with self.subTest(code=answer.code):
                self.bind(answer)
                self.assertEqual("", SELF._own_binding("homestead"))
                self.assertEqual([], self.sends.calls)

    def test_own_binding_drops_other_accounts_through_its_own_sender(self):
        self.bind(self.binding("homestead", "intruder"))
        self.assertEqual("its binding no longer names intruder", SELF._own_binding("homestead"))
        self.assertEqual(["PUT"], [call[0] for call in self.sends.calls])

    def test_get_takes_only_404_as_missing(self):
        self.bind(http_error(404))
        self.assertIsNone(SELF._get("/x"))
        self.bind(http_error(500))
        with self.assertRaises(urllib.error.HTTPError):
            SELF._get("/x")


class DefaultPriorityTests(SharedClientUnused):
    def test_no_scheduling_api_means_no_default_class(self):
        self.assertIsNone(BOOTSTRAP.default_priority(Paths(http_error(404))))

    def test_any_other_failure_or_answer_it_cannot_use_is_unread(self):
        for answer in (http_error(403), http_error(500), RuntimeError("down"), None, []):
            with self.subTest(answer=answer):
                self.assertEqual(BOOTSTRAP.UNREAD, BOOTSTRAP.default_priority(Paths(answer)))

    def test_lowest_global_default_wins(self):
        classes = {"items": [
            {"metadata": {"name": "high"}, "value": 100, "globalDefault": True},
            {"metadata": {"name": "low"}, "value": 10, "globalDefault": True, "preemptionPolicy": "Never"},
            {"metadata": {"name": "other"}, "value": 1}]}
        self.assertEqual({"name": "low", "value": 10, "preemptionPolicy": "Never"},
                         BOOTSTRAP.default_priority(Paths(classes)))
        self.assertIsNone(BOOTSTRAP.default_priority(Paths({"items": []})))


class HeldReadTests(SharedClientUnused):
    """Readers that hold the job on anything but an answer or a 404."""
    def readers(self):
        return (
            ("setup", lambda read, path: BOOTSTRAP.Setup._get(types.SimpleNamespace(read=read), path),
             "Helper setup inventory is unavailable"),
            ("prepare", PREPARE._get, "Destination preparation inventory is unavailable"),
            ("journal", lambda read, path: JOURNAL.Journal._get(types.SimpleNamespace(read=read), path),
             "Storage resource state could not be read"),
        )

    def test_404_is_missing_an_answer_is_kept_and_anything_else_holds(self):
        for name, get, message in self.readers():
            with self.subTest(reader=name):
                read = Paths({"metadata": {"name": "present"}}, missing=http_error(404))
                self.assertIsNone(get(read, "/missing"))
                self.assertEqual("present", get(read, "/present")["metadata"]["name"])
                self.assertEqual(["/missing", "/present"], [call[0] for call in read.calls])
                for failure in (http_error(403), http_error(500), RuntimeError("down"), ValueError("bad")):
                    with self.subTest(failure=failure):
                        with self.assertRaisesRegex(JOURNAL.Held, message) as caught:
                            get(Paths(failure), "/broken")
                        self.assertIsNone(caught.exception.__cause__)


class FenceCompletedReadTests(SharedClientUnused):
    """A completed move's anchor: a 404 is gone (the check goes on to the
    Deployment), its own uid is kept, any other failure raises."""
    ANCHOR = "/api/v1/namespaces/homestead/configmaps/anchor"

    def inspect(self, anchor):
        class ReachedDeployment(Exception):
            pass
        reads = Paths(anchor, deployments=ReachedDeployment())
        saved = types.SimpleNamespace(state={"operation": "op"}, path=self.ANCHOR,
                                      handle=lambda: {"uid": "a1"})
        fence = object.__new__(FENCE.Fence)
        fence.directory, fence.namespace, fence.deployment, fence.read = "/nowhere", "homestead", "homestead", reads
        with mock.patch.object(FENCE, "read_marker", lambda directory: None), \
                mock.patch.object(FINISH, "read", lambda *a: ({}, saved)):
            try:
                fence._inspect()
            except ReachedDeployment:
                return [call[0] for call in reads.calls]

    def test_a_gone_or_own_anchor_goes_on_to_the_deployment(self):
        own = {"metadata": {"uid": "a1", "resourceVersion": "1"}}
        for anchor in (http_error(404), own):
            with self.subTest(anchor=anchor):
                self.assertEqual([self.ANCHOR], self.inspect(anchor)[:1])
                self.assertEqual(2, len(self.inspect(anchor)))

    def test_any_other_failure_raises(self):
        for code in (403, 500):
            with self.subTest(code=code):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.inspect(http_error(code))
                self.assertEqual(code, caught.exception.code)


class FinishReadTests(SharedClientUnused):
    def test_404_is_missing_and_anything_else_raises(self):
        read = Paths({"metadata": {"name": "present"}}, missing=http_error(404))
        self.assertIsNone(FINISH._get(read, "/missing"))
        self.assertEqual("present", FINISH._get(read, "/present")["metadata"]["name"])
        self.assertEqual(2, len(read.calls))
        with self.assertRaises(urllib.error.HTTPError) as caught:
            FINISH._get(Paths(http_error(500)), "/broken")
        self.assertEqual(500, caught.exception.code)


class TransferCleanupTests(SharedClientUnused):
    OBJ = {"metadata": {"name": "snap", "uid": "u1", "resourceVersion": "7"}}

    def test_optional_takes_404_as_missing_and_still_checks_the_answer(self):
        read = Paths(self.OBJ, missing=http_error(404))
        self.assertIsNone(CLEANUP.optional(read, "/missing"))
        self.assertEqual(self.OBJ, CLEANUP.optional(read, "/present"))
        for answer in (None, {}, {"metadata": "x"}, []):
            with self.subTest(answer=answer):
                with self.assertRaisesRegex(ValueError, "could not verify the resource inventory"):
                    CLEANUP.optional(Paths(answer), "/odd")
        with self.assertRaises(urllib.error.HTTPError):
            CLEANUP.optional(Paths(http_error(500)), "/broken")

    def test_items_takes_404_as_empty_only_when_asked_and_still_checks_the_answer(self):
        listing = {"items": [self.OBJ], "metadata": {}}
        self.assertEqual([self.OBJ], CLEANUP.items(Paths(listing), "/list"))
        self.assertEqual([self.OBJ], CLEANUP.items(Paths(listing), "/list", missing=True))
        self.assertEqual([], CLEANUP.items(Paths(http_error(404)), "/list", missing=True))
        with self.assertRaises(urllib.error.HTTPError):
            CLEANUP.items(Paths(http_error(404)), "/list")
        for missing in (False, True):
            with self.assertRaises(urllib.error.HTTPError):
                CLEANUP.items(Paths(http_error(500)), "/list", missing=missing)
            for answer in (None, {"items": None}, {"items": [], "metadata": {"continue": "c"}}, {"items": [{}]}):
                with self.subTest(missing=missing, answer=answer):
                    with self.assertRaisesRegex(ValueError, "complete resource inventory"):
                        CLEANUP.items(Paths(answer), "/list", missing=missing)

    def test_delete_passes_over_what_is_already_gone_and_raises_on_anything_else(self):
        sends = Paths(http_error(404))
        self.assertTrue(CLEANUP.delete(Paths(http_error(404)), sends, "/snap", self.OBJ))
        method, path, body = sends.calls[0]
        self.assertEqual(("DELETE", "/snap"), (method, path))
        self.assertEqual({"uid": "u1", "resourceVersion": "7"}, body["preconditions"])
        # Deleted, but still there while finalizers run.
        self.assertFalse(CLEANUP.delete(Paths(self.OBJ), Paths({}), "/snap", self.OBJ))
        for code in (403, 409, 500):
            with self.subTest(code=code):
                reads = Paths(http_error(404))
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    CLEANUP.delete(reads, Paths(http_error(code)), "/snap", self.OBJ)
                self.assertEqual(code, caught.exception.code)
                self.assertEqual([], reads.calls)


if __name__ == "__main__":
    unittest.main()
