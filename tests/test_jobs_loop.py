"""Only the leader advances jobs, in one loop (#622 step 6).

A viewer's GET /api/operations reads the saved jobs and nothing more: no lock,
no resolver, no write. The leader's jobs loop runs the resolvers every 3 s
while a job is active and every 20 s otherwise; a standby replica's loop does
nothing. Code that needs a job moved on sooner wakes the loop."""
import ast
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tests"))

import homestead_operations as OPS
from fakes.job_store import store_writes


class JobsLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.object(OPS, "DATA_DIR", self.tmp))
        self.saved = dict(OPS.RESOLVERS)
        self.addCleanup(lambda: (OPS.RESOLVERS.clear(), OPS.RESOLVERS.update(self.saved)))
        self.resolved = []

        def resolver(item):
            self.resolved.append(item["id"])
            return "running", 50, "Halfway"
        OPS.RESOLVERS["loop-test"] = resolver
        self.op = OPS.start("loop-test", "A job", {"kind": "Thing", "name": "t", "namespace": "lab"}, "/", {})["id"]

    def get_operations(self):
        return OPS.ROUTES[("GET", "/api/operations")][1](None)

    def test_a_viewers_get_runs_no_resolver_and_writes_nothing(self):
        with store_writes(OPS) as written:
            jobs = self.get_operations()
        self.assertEqual([], self.resolved)
        self.assertEqual([], written)
        self.assertEqual([self.op], [job["id"] for job in jobs])

    def test_a_viewers_get_takes_no_lock(self):
        with mock.patch.object(OPS.SHARED.SharedLock, "__enter__", side_effect=AssertionError("locked")):
            self.get_operations()

    def test_the_leader_advances_an_active_job_and_asks_again_in_3_s(self):
        self.assertEqual(OPS.ACTIVE_SECONDS, OPS.tick(lambda: True))
        self.assertEqual([self.op], self.resolved)
        self.assertEqual("Halfway", next(i for i in OPS.stored() if i["id"] == self.op)["message"])

    def test_a_standby_replica_advances_nothing(self):
        with store_writes(OPS) as written:
            OPS.tick(lambda: False)
        self.assertEqual([], self.resolved)
        self.assertEqual([], written)

    def test_with_nothing_active_the_leader_asks_again_in_20_s(self):
        OPS.RESOLVERS["loop-test"] = lambda item: ("succeeded", 100, "Done")
        OPS.tick(lambda: True)
        self.assertEqual(OPS.IDLE_SECONDS, OPS.tick(lambda: True))
        self.assertEqual((3, 20), (OPS.ACTIVE_SECONDS, OPS.IDLE_SECONDS))

    def test_a_failure_in_a_tick_does_not_stop_the_loop(self):
        with mock.patch.object(OPS, "list_operations", side_effect=RuntimeError("store unreadable")):
            self.assertEqual(OPS.IDLE_SECONDS, OPS.tick(lambda: True))

    def test_advance_soon_wakes_the_loop(self):
        woke = threading.Event()
        stop = threading.Event()
        with mock.patch.object(OPS, "tick", side_effect=lambda is_leader: woke.set() or 3600):
            thread = threading.Thread(target=OPS.run_jobs, args=(lambda: True, stop), daemon=True)
            thread.start()
            self.assertTrue(woke.wait(2))
            woke.clear()
            OPS.advance_soon()
            self.assertTrue(woke.wait(2), "the loop did not wake")
            stop.set()
            OPS.advance_soon()
            thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_starting_a_job_wakes_the_loop(self):
        OPS._wake.clear()
        OPS.start("loop-test", "Another", {"kind": "Thing", "name": "u", "namespace": "lab"}, "/", {})
        self.assertTrue(OPS._wake.is_set(), "a new job would wait up to 20 s for its first step")

    def test_resuming_a_job_wakes_the_loop(self):
        with OPS.transaction() as tx:
            items = tx.items
            next(i for i in items if i["id"] == self.op).update(status="failed")
            tx.save(items)
        self.enterContext(mock.patch.dict(OPS.RESUMABLE, {"loop-test": lambda item: ""}))
        OPS._wake.clear()
        OPS.resume(self.op)
        self.assertTrue(OPS._wake.is_set())

    def test_a_finished_cancel_wakes_the_loop(self):
        OPS._wake.clear()
        OPS.cancel(self.op)
        self.assertTrue(OPS._wake.is_set())

    def test_the_get_lists_newest_first_as_before(self):
        second = OPS.start("loop-test", "Another", {"kind": "Thing", "name": "u", "namespace": "lab"}, "/", {})["id"]
        with mock.patch.object(OPS, "_now", return_value="2099-01-01T00:00:00Z"):
            third = OPS.start("loop-test", "Newest", {"kind": "Thing", "name": "v", "namespace": "lab"}, "/", {})["id"]
        self.assertEqual(third, self.get_operations()[0]["id"])
        self.assertIn(second, [job["id"] for job in self.get_operations()])


class NoRequestAdvancesJobsTests(unittest.TestCase):
    """Server code reads the saved jobs (snapshot()); only the loop calls
    list_operations(), the function that runs the resolvers."""

    def test_only_the_jobs_loop_calls_list_operations(self):
        callers = []
        for path in sorted((ROOT / "server").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) == "list_operations":
                    callers.append(f"{path.name}:{node.lineno}")
        self.assertEqual([], [c for c in callers if not c.startswith("homestead_operations.py:")], callers)


if __name__ == "__main__":
    unittest.main()
