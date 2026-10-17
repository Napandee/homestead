"""What the leader did, shown by every replica (homestead_leader_state)."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_heartbeats as HEARTBEATS
import homestead_housekeeping as HOUSEKEEPING
import homestead_leader_state as LEADER_STATE
import homestead_mqtt as MQTT
import homestead_outage as OUTAGE
import homestead_self_health as SELF_HEALTH
import homestead_shared as SHARED


class SharedDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved_dir = SHARED.DIR
        SHARED.bind(self.tmp.name)
        LEADER_STATE._written.clear()

    def tearDown(self):
        SHARED.bind(self.saved_dir)
        LEADER_STATE._written.clear()
        self.tmp.cleanup()


class LeaderStateTests(SharedDir):
    def test_written_on_a_change_and_otherwise_at_most_every_so_often(self):
        self.assertTrue(LEADER_STATE.put("x", {"n": 1}, every=60, key="a", now=1000))
        self.assertFalse(LEADER_STATE.put("x", {"n": 2}, every=60, key="a", now=1030))
        self.assertEqual(({"n": 1}, 1000), LEADER_STATE.get("x", now=1030))
        self.assertTrue(LEADER_STATE.put("x", {"n": 3}, every=60, key="b", now=1031))    # the key changed
        self.assertTrue(LEADER_STATE.put("x", {"n": 4}, every=60, key="b", now=1092))
        self.assertEqual({"n": 4}, LEADER_STATE.get("x", now=1100)[0])
        self.assertEqual((None, 0), LEADER_STATE.get("x", max_age=60, now=1200))
        self.assertEqual((None, 0), LEADER_STATE.get("missing"))

    def test_sections_are_kept_apart(self):
        LEADER_STATE.put("a", 1, now=10)
        LEADER_STATE.put("b", 2, now=20)
        self.assertEqual(((1, 10), (2, 20)), (LEADER_STATE.get("a", now=30), LEADER_STATE.get("b", now=30)))

    def test_a_failed_write_is_tried_again_next_time(self):
        with mock.patch.object(SHARED, "write_json", side_effect=OSError("read-only")):
            self.assertFalse(LEADER_STATE.put("x", 1, every=60, key="a", now=1000))
        self.assertTrue(LEADER_STATE.put("x", 1, every=60, key="a", now=1001))


class MqttStatusTests(SharedDir):
    def setUp(self):
        super().setUp()
        self.saved = dict(MQTT.STATUS)

    def tearDown(self):
        MQTT.STATUS.clear()
        MQTT.STATUS.update(self.saved)
        super().tearDown()

    def test_a_standby_replica_shows_the_leaders_status_while_it_is_fresh(self):
        MQTT.STATUS.update(state="publishing", detail="publishing to 192.0.2.177:1883 every 60s", last_publish=1000, error="")
        MQTT._share(now=1000)
        MQTT.STATUS.update(state="standby", detail="another Homestead replica is publishing", last_publish=0)
        shown = MQTT.shown_status(60, now=1100)
        self.assertEqual(("publishing", 1000), (shown["state"], shown["last_publish"]))
        self.assertEqual("standby", MQTT.shown_status(60, now=1000 + 600)["state"])

    def test_a_publishing_replica_shows_its_own(self):
        MQTT.STATUS.update(state="error", error="the broker refused the connection")
        self.assertEqual("error", MQTT.shown_status(60, now=5)["state"])


class HeartbeatTests(SharedDir):
    def setUp(self):
        super().setUp()
        self.saved = dict(HEARTBEATS.HEART)
        HEARTBEATS.HEART.clear()

    def tearDown(self):
        HEARTBEATS.HEART.clear()
        HEARTBEATS.HEART.update(self.saved)
        super().tearDown()

    def test_the_leaders_beats_are_shared_and_a_failure_at_once(self):
        with mock.patch("time.time", return_value=1000):
            HEARTBEATS.beat("auto-updates", 60, leader_only=True)
            HEARTBEATS.beat("sampler", 30)                     # every replica's own: not shared
        self.assertEqual({"auto-updates"}, set(HEARTBEATS.leader_beats(max_age=None)))
        with mock.patch("time.time", return_value=1010):
            HEARTBEATS.beat("auto-updates", 60, "no registry", leader_only=True)
        self.assertEqual("no registry", HEARTBEATS.leader_beats(max_age=None)["auto-updates"]["error"])

    def test_the_health_card_shows_the_leaders_tasks_on_another_replica(self):
        shared = {"auto-updates": {"every": 60, "leader_only": True, "last_ok": 990, "error": "", "error_at": 0},
                  "moves": {"every": 5, "leader_only": True, "last_ok": 100, "error": "", "error_at": 0}}
        local = {"auto-updates": {"every": 60, "leader_only": True, "last_ok": 0, "error": "", "error_at": 0}}
        rows = self.loops(local, shared, leading=False)
        self.assertEqual("ok", rows["auto-updates"]["state"])
        self.assertEqual("late", rows["moves"]["state"])
        # nothing shared (one replica, or the leader is gone): standby, as before
        self.assertEqual("standby", self.loops(local, {}, leading=False)["auto-updates"]["state"])

    def loops(self, local, shared, leading):
        with mock.patch.object(SELF_HEALTH, "LOOP_WORDS", {"auto-updates": "Auto-updates", "moves": "Moves"}):
            return {row["name"]: row for row in SELF_HEALTH.loop_states(local, shared, leading, 1000)}


class OtherSharedResultsTests(SharedDir):
    def test_housekeeping_shows_the_leaders_last_tidy(self):
        saved = dict(HOUSEKEEPING._last)
        try:
            LEADER_STATE.put("housekeeping", {"at": 5000, "result": {"freed": 10, "notes": ["trimmed"], "pressure": False}})
            HOUSEKEEPING._last.update(at=0, result=None)
            with mock.patch.object(HOUSEKEEPING, "volume", lambda: {}), mock.patch.object(HOUSEKEEPING, "_all_files", lambda: []), \
                    mock.patch.object(HOUSEKEEPING, "_files", lambda pattern: []):
                report = HOUSEKEEPING.report()
            self.assertEqual((5000, ["trimmed"]), (report["last_at"], report["last"]["notes"]))
        finally:
            HOUSEKEEPING._last.clear()
            HOUSEKEEPING._last.update(saved)

    def test_outage_webhooks_show_the_newer_of_the_leaders_and_this_replicas(self):
        saved = dict(OUTAGE._last_webhook)
        try:
            LEADER_STATE.put("outage-webhooks", {"app:lab/frigate": {"at": 200, "ok": True, "event": "down"},
                                                  "vm:vms/win11": {"at": 100, "ok": True, "event": "down"}})
            OUTAGE._last_webhook.clear()
            OUTAGE._last_webhook["vm:vms/win11"] = {"at": 150, "ok": False, "event": "up", "error": "timeout"}
            with mock.patch.object(OUTAGE, "_load", lambda: {"items": {}}):
                report = OUTAGE.report()
            self.assertEqual(200, report["app:lab/frigate"]["webhook"]["at"])
            self.assertEqual("timeout", report["vm:vms/win11"]["webhook"]["error"])
        finally:
            OUTAGE._last_webhook.clear()
            OUTAGE._last_webhook.update(saved)


if __name__ == "__main__":
    unittest.main()
