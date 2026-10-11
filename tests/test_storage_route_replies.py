"""Storage routes that answer more than 200 keep their status codes now
their modules answer them (#363)."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_routes as ROUTES


def call(method, path, body):
    route = ROUTES.find(method, path)
    return route.handler(ROUTES.Request(method, path, {}, body, "ada", "admin"))


class StorageReplies(unittest.TestCase):
    def test_a_second_rebalance_is_a_409_with_the_running_job(self):
        import homestead_rebalance as REBALANCE
        running = {"kind": REBALANCE.KIND, "status": "running", "id": "r1"}
        with mock.patch.object(REBALANCE.OPS, "list_operations", return_value=[running]):
            answer = call("POST", "/api/longhorn/rebalance", {})
        self.assertEqual(409, answer.code)
        self.assertEqual(running, answer.body["operation"])

    def test_a_changed_rebalance_review_is_a_409_with_the_plan_now(self):
        import homestead_rebalance as REBALANCE
        with mock.patch.object(REBALANCE.OPS, "list_operations", return_value=[]), \
                mock.patch.object(REBALANCE, "plan", return_value={"review_token": "new", "moves": []}):
            answer = call("POST", "/api/longhorn/rebalance", {"review_token": "old"})
        self.assertEqual((409, "new"), (answer.code, answer.body["plan"]["review_token"]))

    def test_transfers_refused_is_a_409_and_an_unreachable_cluster_a_502(self):
        import homestead_objectstore as OBJECTS
        with mock.patch.object(OBJECTS, "set_transfers", side_effect=ValueError("still running")):
            self.assertEqual((409, {"error": "still running"}),
                             tuple(call("POST", "/api/objectstore/transfers", {"allow": True})[:2]))
        with mock.patch.object(OBJECTS, "set_transfers", side_effect=OBJECTS.MOVE.Unreachable("gone")):
            self.assertEqual(502, call("POST", "/api/objectstore/transfers", {"allow": True}).code)
        with mock.patch.object(OBJECTS, "set_transfers", side_effect=OBJECTS.MOVE_SOURCE.PendingRecovery("wait")):
            self.assertEqual(503, call("POST", "/api/objectstore/transfers", {"allow": True}).code)

    def test_a_restore_onto_an_existing_claim_is_a_409_and_starts_nothing(self):
        import homestead_longhorn as LH
        plan = {"conflict": {"message": "media already exists"}}
        with mock.patch.object(LH, "restore_plan", return_value=plan), \
                mock.patch.object(LH, "restore_backup") as restore:
            answer = call("POST", "/api/lh/restore", {"backup": "b1", "name": "media"})
        self.assertEqual((409, "media already exists"), (answer.code, answer.body["error"]))
        restore.assert_not_called()


if __name__ == "__main__":
    unittest.main()
