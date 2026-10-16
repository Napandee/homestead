import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_history as H

DAY = 86400


def overview(ready, boot="boot-1"):
    return {"cpu_pct": 1, "mem_pct": 1, "nodes_ready": int(ready), "nodes_total": 1,
            "nodes": [{"name": "node1", "status": "Ready" if ready else "NotReady", "cpu_pct": 1, "mem_pct": 1,
                       "boot_id": boot}]}


class NodeUptimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = H.DATA_DIR
        H.bind(self.tmp.name)
        self.start = 1_700_000_000 - 1_700_000_000 % DAY

    def tearDown(self):
        H.DATA_DIR = self.saved
        self.tmp.cleanup()

    def run_for(self, minutes, down_between=(), boot_changes_at=None):
        for i in range(0, minutes, 5):
            t = self.start + i * 60
            boot = "boot-2" if boot_changes_at is not None and i >= boot_changes_at else "boot-1"
            H.record(overview(not (down_between and down_between[0] <= i < down_between[1]), boot), now=t)
        return self.start + minutes * 60

    def test_an_outage_is_found_to_the_five_minutes(self):
        now = self.run_for(180, down_between=(60, 90))
        node = H.uptime(now)["nodes"]["node1"]
        outage = node["outages"][-1]
        self.assertEqual((self.start + 3600, 30 * 60, True), (outage["start"], outage["down_s"], outage["exact"]))
        self.assertFalse(outage["ongoing"])
        self.assertAlmostEqual(83.333, node["windows"]["24h"], places=2)

    def test_a_node_down_now_is_an_ongoing_outage(self):
        now = self.run_for(60, down_between=(40, 60))
        self.assertTrue(H.uptime(now)["nodes"]["node1"]["outages"][-1]["ongoing"])

    def test_a_new_boot_id_is_a_reboot(self):
        now = self.run_for(60, boot_changes_at=30)
        self.assertEqual([{"t": self.start + 30 * 60, "planned": False}], H.uptime(now)["nodes"]["node1"]["reboots"])

    def test_always_up_is_a_hundred(self):
        now = self.run_for(60)
        node = H.uptime(now)["nodes"]["node1"]
        self.assertEqual(100.0, node["windows"]["24h"])
        self.assertEqual([], node["outages"])
        self.assertEqual(90, len(node["days"]))


class MaintenanceTests(NodeUptimeTests):
    """Downtime Homestead caused, recorded apart from the samples (#535)."""

    def at(self, minute):
        return self.start + minute * 60

    def test_open_close_and_idempotence(self):
        first = H.maintenance_open("node1", "OS update", "op-1", now=self.at(60))
        self.assertEqual(first, H.maintenance_open("node1", "OS update", "op-1", now=self.at(61)))
        self.assertEqual(1, len(H.maintenance_events("node1")))
        closed = H.maintenance_close("op-1", now=self.at(80), outcome="returned")
        self.assertEqual((self.at(80), "returned"), (closed["end"], closed["outcome"]))
        self.assertEqual(closed, H.maintenance_close("op-1", now=self.at(90), outcome="limit"))
        self.assertIsNone(H.maintenance_close("nope"))

    def test_excluding_maintenance_and_split_outage(self):
        H.maintenance_open("node1", "OS update", "op-1", now=self.at(60))
        H.maintenance_close("op-1", now=self.at(80), outcome="limit")
        now = self.run_for(180, down_between=(60, 90))
        node = H.uptime(now)["nodes"]["node1"]
        self.assertAlmostEqual(83.333, node["windows"]["24h"], places=2)
        self.assertAlmostEqual(93.75, node["windows_excluding_maintenance"]["24h"], places=2)
        self.assertEqual(1200, node["maintenance_s"]["24h"])
        first, second = node["outages"][-2:]
        self.assertEqual((self.at(60), 1200, "OS update"), (first["start"], first["down_s"], first["maintenance"]))
        self.assertEqual((self.at(80), 600), (second["start"], second["down_s"]))
        self.assertNotIn("maintenance", second)
        self.assertTrue(node["days"][-1]["maintenance"])
        self.assertFalse(node["days"][-2]["maintenance"])

    def test_planned_and_unexpected_reboots(self):
        H.maintenance_open("node1", "host reboot", "op-1", now=self.at(60))
        H.maintenance_close("op-1", now=self.at(65), outcome="returned")
        for minute in range(0, 180, 5):
            boot = "b1" if minute < 70 else "b2" if minute < 120 else "b3"
            H.record(overview(True, boot), now=self.at(minute))
        reboots = H.uptime(self.at(180))["nodes"]["node1"]["reboots"]
        self.assertEqual([{"t": self.at(70), "planned": True}, {"t": self.at(120), "planned": False}], reboots)

    def test_a_typical_os_update_leaves_no_outage(self):
        """A six-minute OS update whose host was seen down in two samples is
        maintenance through and through, in the samples and in the hourly rows
        (#535 review I1)."""
        H.maintenance_open("node1", "OS update", "op-1", now=self.at(61))
        H.maintenance_close("op-1", now=self.at(67), outcome="returned")
        now = self.run_for(180, down_between=(60, 70))
        node = H.uptime(now)["nodes"]["node1"]
        self.assertEqual(100.0, node["windows_excluding_maintenance"]["24h"])
        self.assertEqual([], [o for o in node["outages"] if not o.get("maintenance")])
        later = self.start + 3 * DAY
        H.record(overview(True), now=later)
        node = H.uptime(later)["nodes"]["node1"]
        self.assertEqual(100.0, node["windows_excluding_maintenance"]["90d"], "the hourly rows too")
        self.assertEqual([], [o for o in node["outages"] if not o.get("maintenance")])

    def test_a_day_with_maintenance_still_shows_its_outage(self):
        H.maintenance_open("node1", "OS update", "op-1", now=self.at(10))
        H.maintenance_close("op-1", now=self.at(16), outcome="returned")
        now = self.run_for(180, down_between=(60, 120))
        today = H.uptime(now)["nodes"]["node1"]["days"][-1]
        self.assertTrue(today["maintenance"])
        self.assertAlmostEqual(100 * 120 / 180, today["up_excluding_maintenance"], delta=4)

    def test_overlapping_events_count_once(self):
        H.maintenance_open("node1", "OS update", "op-1", now=self.at(60))
        H.maintenance_close("op-1", now=self.at(80))
        H.maintenance_open("node1", "Harvester maintenance", "harvester:node1:x", now=self.at(70))
        H.maintenance_close("harvester:node1:x", now=self.at(90))
        node = H.uptime(self.run_for(180, down_between=(60, 90)))["nodes"]["node1"]
        self.assertEqual(1800, node["maintenance_s"]["24h"])
        self.assertEqual(100.0, node["windows_excluding_maintenance"]["24h"])

    def test_a_forgotten_power_event_closes_at_the_ceiling(self):
        H.maintenance_open("node1", "host reboot", "op-1", now=self.start)
        H.maintenance_open("node2", "cluster shutdown", "cs:run:node2", now=self.start)
        later = self.start + H.MAX_OPEN + 1
        self.assertIsNone(H.maintenance_open_for("node1", now=later))
        H.record(overview(True), now=later)
        stored = H.maintenance_events("node1")[0]
        self.assertEqual((self.start + H.MAX_OPEN, "limit"), (stored["end"], stored["outcome"]))
        self.assertIsNotNone(H.maintenance_open_for("node2", now=later))

    def test_record_keeps_events_and_old_files_read_as_none(self):
        import json, os
        with open(os.path.join(self.tmp.name, H.FILE), "w") as handle:
            json.dump({"fine": [], "coarse": [], "boots": {}, "events": []}, handle)
        self.assertEqual([], H.maintenance_events("node1"))
        H.maintenance_open("node1", "OS update", "op-1", now=self.at(10))
        H.record(overview(True), now=self.at(15))
        self.assertEqual(1, len(H.maintenance_events("node1")))
        old = self.start - H.COARSE_KEEP - 10
        H.maintenance_open("node1", "OS update", "op-old", now=old - 60)
        H.maintenance_close("op-old", now=old)
        H.maintenance_open("node1", "cluster shutdown", "cs:old:node1", now=old - 60)
        H.record(overview(True), now=self.start)
        self.assertEqual({"op-1", "cs:old:node1"}, {e["job"] for e in H.maintenance_events("node1")})

    def test_end_is_never_before_start(self):
        H.maintenance_open("node1", "host reboot", "op-1", now=self.at(10))
        self.assertEqual(self.at(10), H.maintenance_close("op-1", now=self.at(0))["end"])

    def test_the_api_lists_events(self):
        H.maintenance_open("node1", "OS update", "op-1", now=self.at(60))
        H.maintenance_close("op-1", now=self.at(66), outcome="returned")
        node = H.uptime(self.run_for(90))["nodes"]["node1"]
        self.assertEqual([{"start": self.at(60), "end": self.at(66), "reason": "OS update", "job": "op-1", "ongoing": False}],
                         node["maintenance"])


    def test_a_malformed_entry_is_skipped_not_fatal(self):
        import json, os
        H.record(overview(True), now=self.at(0))
        path = os.path.join(self.tmp.name, H.FILE)
        data = json.load(open(path))
        good = {"id": "op-1", "node": "node1", "start": self.at(5), "end": self.at(10), "reason": "OS update", "job": "op-1", "outcome": "returned"}
        data["maintenance"] = [{"bad": 1}, "x", {"job": "op-2", "node": "node1", "start": "soon"}, good]
        json.dump(data, open(path, "w"))
        self.assertEqual([good], H.maintenance_events("node1"))
        H.record(overview(True), now=self.at(15))
        self.assertIsNone(H.maintenance_open_for("node1"))
        self.assertEqual(1, len(H.uptime(self.at(20))["nodes"]["node1"]["maintenance"]))

    def test_overlapping_events_name_both_reasons(self):
        H.maintenance_open("node1", "OS update", "op-1", now=self.at(60))
        H.maintenance_close("op-1", now=self.at(80))
        H.maintenance_open("node1", "Harvester maintenance", "harvester:node1:x", now=self.at(70))
        H.maintenance_close("harvester:node1:x", now=self.at(90))
        node = H.uptime(self.run_for(180, down_between=(60, 90)))["nodes"]["node1"]
        self.assertEqual({"OS update, Harvester maintenance"}, {o["maintenance"] for o in node["outages"] if o.get("maintenance")})
if __name__ == "__main__":
    unittest.main()
