"""/readyz (#736): open, cheap, one bit; the self-data fence answers it as it
answers /healthz."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import server
import homestead_fleet as FLEET
import homestead_metrics as METRICS
import homestead_readyz as READYZ
import homestead_routes as MODULE_ROUTES
import homestead_self_data_fence as FENCE
import homestead_shared as SHARED
from homestead_storage_journal import Held


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class ReadyzChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.clock = Clock()
        self.api = mock.Mock(return_value='{"major": "1"}')
        for patch in (mock.patch.dict(READYZ._state, READYZ.START),
                      mock.patch.object(READYZ, "_clock", self.clock),
                      mock.patch.object(READYZ, "DATA_DIR", self.temp.name),
                      mock.patch.object(READYZ.KUBE, "raw_get", self.api)):
            patch.start(); self.addCleanup(patch.stop)

    def answer(self):
        reply = READYZ.answer()
        return reply.code, reply.body

    def test_ready_when_the_api_answers_and_data_is_writable(self):
        self.assertEqual((200, {"ready": True}), self.answer())
        self.api.assert_called_once_with("/version", timeout=2)
        self.assertEqual([], list(Path(self.temp.name).iterdir()), "the probe file is removed again")

    def test_not_ready_before_the_first_check_has_finished(self):
        """Another caller is running the first check: no optimistic answer."""
        with READYZ._lock:
            self.assertEqual((503, {"ready": False}), self.answer())
        self.api.assert_not_called()

    def test_not_ready_when_the_api_last_answered_more_than_30_seconds_ago(self):
        self.assertEqual(200, self.answer()[0])
        self.api.side_effect = OSError("connection refused")
        self.clock.now += 25
        self.assertEqual(200, self.answer()[0], "a failure within 30 s of the last answer is not yet unready")
        self.clock.now += 6
        self.assertEqual((503, {"ready": False}), self.answer())

    def test_not_ready_when_data_cannot_be_written(self):
        with mock.patch.object(READYZ, "DATA_DIR", os.path.join(self.temp.name, "missing")):
            self.assertEqual((503, {"ready": False}), self.answer())

    def test_a_burst_of_probes_costs_one_api_call_per_10_s_and_one_write_per_30_s(self):
        with mock.patch.object(READYZ, "_write_probe", wraps=READYZ._write_probe) as write:
            for _ in range(50):
                self.answer()
                self.clock.now += 0.19
            self.assertEqual(1, self.api.call_count)
            self.assertEqual(1, write.call_count)
            self.clock.now = 1010.0
            self.answer()
            self.assertEqual(2, self.api.call_count)
            self.assertEqual(1, write.call_count)
            self.clock.now = 1030.0
            self.answer()
            self.assertEqual(2, write.call_count)

    def test_the_probe_write_goes_through_the_fence(self):
        """The fence holds after the guard let /readyz through: no write, a
        clean not-ready, never a 500."""
        with mock.patch.object(SHARED, "WRITE_SCOPE", mock.Mock(side_effect=Held("held"))) as scope:
            self.assertEqual((503, {"ready": False}), self.answer())
        scope.assert_called_once_with(os.path.join(self.temp.name, f".readyz-{READYZ.POD_NAME}"))
        self.assertEqual([], list(Path(self.temp.name).iterdir()))

    def test_the_probe_write_checks_the_write_guard(self):
        with mock.patch.object(SHARED, "WRITE_GUARD", mock.Mock(side_effect=Held("held"))) as guard:
            self.assertEqual((503, {"ready": False}), self.answer())
        guard.assert_called_once()
        self.assertEqual([], list(Path(self.temp.name).iterdir()))

    @unittest.skipUnless(FENCE.fcntl is not None, "Linux shared locking")
    def test_the_probe_write_does_not_race_a_freeze(self):
        """A freeze drains activity; the probe's write is activity, so while
        frozen it is refused, not written past the freeze."""
        locks = tempfile.TemporaryDirectory(); self.addCleanup(locks.cleanup)
        mine, mover = FENCE.WriteBarrier(locks.name, lambda: None), FENCE.WriteBarrier(locks.name, lambda: None)
        with mock.patch.object(SHARED, "WRITE_SCOPE", lambda path: mine.activity()):
            with mover.freeze(timeout=.05):
                self.assertEqual((503, {"ready": False}), self.answer())
                self.assertEqual([], list(Path(self.temp.name).iterdir()))
            self.clock.now += READYZ.DATA_EVERY
            self.assertEqual((200, {"ready": True}), self.answer(), "after the freeze it writes again")

    def test_a_probe_file_that_cannot_be_removed_still_counts_as_written(self):
        with mock.patch.object(READYZ.os, "remove", side_effect=OSError("busy")):
            self.assertEqual(200, self.answer()[0])

    def test_without_the_data_probe_only_the_api_is_checked(self):
        """What /metrics asks: the API check, and the last /data result,
        never a write of its own."""
        with mock.patch.object(READYZ, "_write_probe", side_effect=AssertionError("wrote")):
            state = READYZ.state(probe_data=False)
        self.assertEqual({"kube_api": True, "data_writable": None}, state["checks"])
        self.assertFalse(state["ready"])
        self.answer()
        self.assertTrue(READYZ.state(probe_data=False)["checks"]["data_writable"])

    def test_state_has_the_detail_the_answer_withholds(self):
        self.api.side_effect = OSError("down")
        self.answer()
        state = READYZ.state()
        self.assertEqual({"kube_api": False, "data_writable": True}, state["checks"])
        self.assertFalse(state["ready"])

    def test_answered_locally_never_relayed_to_a_linked_cluster(self):
        self.assertIn("/readyz", FLEET.LOCAL)
        self.assertIn("/metrics", FLEET.LOCAL)


def handler(path, method="GET"):
    h = object.__new__(server.H)
    h.command, h.path, h.headers = method, path, {}
    h._send = mock.Mock(); h._who = mock.Mock(return_value=None)
    return h


class ReadyzThroughTheServer(unittest.TestCase):
    """The same request the kubelet or an uptime checker sends: no session, no key."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.fence = mock.Mock()
        self.fence.inspect.return_value = {"mode": "start", "writable": False}
        self.fence.require_write.side_effect = Held("held")
        self.writes = mock.Mock(side_effect=AssertionError("/readyz wrote to /data during the fence"))
        for patch in (mock.patch.object(server, "_self_data_fence", self.fence),
                      mock.patch.object(server, "_self_data_boot_pending", True),
                      mock.patch.object(server, "_self_data_boot_failed", False),
                      mock.patch.object(server.OPS, "DATA_DIR", self.temp.name),
                      mock.patch.object(server.AUTH, "review_signing_key", return_value=b"read-only-fixture"),
                      mock.patch.object(server.CFACCESS, "enabled", return_value=False),
                      mock.patch.dict(READYZ._state, READYZ.START),
                      mock.patch.object(READYZ, "DATA_DIR", self.temp.name),
                      mock.patch.object(READYZ, "_write_probe", self.writes)):
            patch.start(); self.addCleanup(patch.stop)

    def sent(self, path):
        h = handler(path)
        self.assertTrue(h._guard(path), path)
        return h._send.call_args.args[:2]

    def test_answered_without_a_session_or_key(self):
        self.fence.require_write.side_effect = None
        self.writes.side_effect = None
        with mock.patch.object(server, "_self_data_boot_pending", False), \
             mock.patch.object(READYZ.KUBE, "raw_get", return_value="{}"):
            h = handler("/readyz")
            self.assertIsNone(h._guard("/readyz"))
            h.do_GET()
        self.assertEqual((200, {"ready": True}), h._send.call_args.args[:2])
        h._who.assert_not_called()

    def test_boot_handoff_pending_with_a_readable_status(self):
        self.assertEqual((200, {"ok": True, "data_handoff": True, "read_only": True}), self.sent("/healthz"))
        self.assertEqual((200, {"ready": True}), self.sent("/readyz"))

    def test_boot_handoff_held(self):
        with mock.patch.object(server, "self_data_boot_status", side_effect=server.SELF_DATA_FENCE.Held("held")):
            for path in ("/healthz", "/readyz"):
                code, body = self.sent(path)
                self.assertEqual(503, code, path)
            self.assertEqual({"ready": False}, body)

    def test_fence_holds_with_a_recovery_record(self):
        self.fence.recovery.return_value = {"mode": "recovery", "writable": False, "operation": "a" * 24}
        with mock.patch.object(server, "_self_data_boot_pending", False):
            code, body = self.sent("/healthz")
            self.assertEqual(200, code); self.assertTrue(body["recovery"])
            self.assertEqual((200, {"ready": True}), self.sent("/readyz"))

    def test_fence_holds_without_a_recovery_record(self):
        self.fence.recovery.side_effect = RuntimeError("no record")
        with mock.patch.object(server, "_self_data_boot_pending", False):
            self.assertEqual(503, self.sent("/healthz")[0])
            self.assertEqual((503, {"ready": False}), self.sent("/readyz"))

    def test_metrics_while_the_fence_holds_never_writes_to_data(self):
        """/metrics is not in the guard's fence paths: it is read while the
        fence holds, and must not reach the probe write."""
        MODULE_ROUTES.forget("metrics"); self.addCleanup(MODULE_ROUTES.forget, "metrics")
        h = handler("/metrics")
        h._who.return_value = {"user": "viewer1", "role": "viewer", "remember": False, "started": 0}
        h.client_address = ("192.0.2.10", 40000)
        with mock.patch.object(server, "_self_data_boot_pending", False), \
             mock.patch.object(METRICS.SETTINGS_HELPERS, "get_app_settings", return_value={"metrics": {"enabled": True}}), \
             mock.patch.object(READYZ.KUBE, "raw_get", return_value="{}"):
            h.do_GET()
        code, body = h._send.call_args.args[:2]
        self.assertEqual(200, code)
        self.assertIn('homestead_metrics_source_up{source="kube_api"} 1', body)

    def tearDown(self):
        # In every fence state the guard answers first: /readyz never writes
        # its probe file to a volume that is being handed off.
        if self.writes.side_effect is not None:
            self.writes.assert_not_called()
            self.assertEqual([], list(Path(self.temp.name).iterdir()))


if __name__ == "__main__":
    unittest.main()
