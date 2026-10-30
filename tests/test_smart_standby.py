"""The regular SMART read leaves drives the host has spun down asleep (#974)."""
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tests"))

import tempfile

import homestead_alerts as ALERTS
import homestead_app_settings as APP_SETTINGS
import homestead_node_probes as PROBES
import homestead_nodes as NODES
import homestead_smart as SMART
from test_smart_readings import Base, ROWS, _Response, load_helper, probe_pods

CFG = APP_SETTINGS.DEFAULTS["smart"]
AWAKE = {"name": "sda", "available": True, "health": "passed", "temperature_c": 38,
         "power_on_hours": 1200, "pending": 0, "reallocated": 0, "uncorrectable": 0,
         "wear": {"life_pct": 90}}


class Smartctl:
    """A fake smartctl: records every argv; a drive in standby honours -n."""

    def __init__(self, sleeping=True):
        self.argvs, self.sleeping = [], sleeping

    def __call__(self, argv, **_):
        self.argvs.append(argv)
        if self.sleeping and "-n" in argv:
            code = int(argv[argv.index("-n") + 1].split(",")[1])
            out = {"smartctl": {"messages": [{"string": f"Device is in STANDBY mode, exit({code})"}],
                                "exit_status": code}}
            return SimpleNamespace(returncode=code, stdout=json.dumps(out), stderr="")
        out = {"smart_support": {"available": True, "enabled": True},
               "smart_status": {"passed": True}, "temperature": {"current": 40}}
        return SimpleNamespace(returncode=0, stdout=json.dumps(out), stderr="")


class HelperStandbyTests(unittest.TestCase):
    def setUp(self):
        self.smart = load_helper()
        self.smart.device_type = lambda name: "sat"
        self.smart.disks = lambda: [("sda", "/host/dev/sda"), ("sdb", "/host/dev/sdb")]
        self.smart.signature_ok = lambda *_: True
        self.smartctl = Smartctl()
        patch = mock.patch.object(self.smart.subprocess, "run", self.smartctl)
        patch.start()
        self.addCleanup(patch.stop)

    def get(self, path):
        handler = self.smart.H.__new__(self.smart.H)
        handler.path, handler.headers, sent = path, {}, []
        handler.send_json = lambda code, value: sent.append((code, value))
        handler.do_GET()
        return sent[0]

    def test_the_regular_read_of_every_drive_does_not_wake_one(self):
        code, value = self.get("/")
        self.assertEqual(200, code)
        self.assertEqual(2, len(self.smartctl.argvs))
        for argv in self.smartctl.argvs:
            self.assertEqual(["-n", "standby,3"], argv[1:3], argv)

    def test_a_drive_in_standby_is_asleep_not_unavailable(self):
        _, value = self.get("/")
        row = value["disks"][0]
        self.assertTrue(row["asleep"])
        self.assertTrue(row["available"])
        self.assertEqual("", row["unavailable_reason"])
        self.assertIsNone(row["temperature_c"])
        self.assertFalse(row["test"]["active"])

    def test_an_awake_drive_is_read_in_full_by_the_same_read(self):
        self.smartctl.sleeping = False
        _, value = self.get("/")
        row = value["disks"][0]
        self.assertNotIn("asleep", row)
        self.assertEqual(40, row["temperature_c"])

    def test_exit_3_without_a_standby_message_is_not_asleep(self):
        out = json.dumps({"smartctl": {"messages": [{"string": "something else"}], "exit_status": 3}})
        self.smart.subprocess.run = lambda argv, **_: SimpleNamespace(returncode=3, stdout=out, stderr="")
        self.assertNotIn("asleep", self.smart.report("sda", "/host/dev/sda", wake=False))

    def test_a_drive_that_cannot_be_opened_is_still_unavailable(self):
        self.smart.subprocess.run = lambda argv, **_: SimpleNamespace(returncode=2, stdout="", stderr="open failed")
        row = self.smart.report("sda", "/host/dev/sda", wake=False)
        self.assertNotIn("asleep", row)
        self.assertEqual("open failed", row["unavailable_reason"])

    def test_one_drive_asked_for_by_name_is_read_even_asleep(self):
        # Before a self-test, and while it runs, the drive has to answer.
        code, value = self.get("/?disk=sda")
        self.assertEqual(200, code)
        self.assertNotIn("-n", self.smartctl.argvs[0])
        self.assertNotIn("asleep", value["disk"])


class KeepLastTests(Base):
    def asleep(self, name="sda"):
        return {"name": name, "available": True, "asleep": True, "health": "unknown",
                "temperature_c": None, "wear": {"life_pct": None}}

    def test_a_sleeping_drive_keeps_its_last_reading_with_the_temperature_unknown(self):
        SMART.keep_last("node-1", {"sda": dict(AWAKE)})
        row = SMART.keep_last("node-1", {"sda": self.asleep()})["sda"]
        self.assertTrue(row["asleep"])
        self.assertEqual(1200, row["power_on_hours"])
        self.assertEqual("passed", row["health"])
        self.assertIsNone(row["temperature_c"])
        self.assertEqual(int(SMART._wall()), row["read_at"])
        # Still kept on the next sleeping read.
        again = SMART.keep_last("node-1", {"sda": self.asleep()})["sda"]
        self.assertEqual(1200, again["power_on_hours"])

    def test_a_drive_asleep_since_before_any_reading_is_asleep_not_unavailable(self):
        row = SMART.keep_last("node-1", {"sda": self.asleep()})["sda"]
        health = NODES.smart_disk_health(row, CFG)
        self.assertEqual("asleep", health["state"])
        self.assertEqual([], health["issues"])

    def test_a_sleeping_drive_with_bad_sectors_keeps_its_findings(self):
        SMART.keep_last("node-1", {"sda": dict(AWAKE, pending=4)})
        row = SMART.keep_last("node-1", {"sda": self.asleep()})["sda"]
        self.assertEqual("critical", NODES.smart_disk_health(row, CFG)["state"])

    def test_a_sleeping_drive_with_a_clean_reading_is_asleep_not_healthy(self):
        SMART.keep_last("node-1", {"sda": dict(AWAKE)})
        row = SMART.keep_last("node-1", {"sda": self.asleep()})["sda"]
        health = NODES.smart_disk_health(row, CFG)
        self.assertEqual("asleep", health["state"])
        self.assertEqual(90, health["life_pct"])
        self.assertEqual(row["read_at"], health["read_at"])
        self.assertIsNone(NODES.smart_disk_health(self.asleep(), CFG)["read_at"])

    def test_a_drive_gone_from_the_host_is_forgotten(self):
        SMART.keep_last("node-1", {"sda": dict(AWAKE)})
        SMART.keep_last("node-1", {"sdb": dict(AWAKE, name="sdb")})
        row = SMART.keep_last("node-1", {"sda": self.asleep()})["sda"]
        self.assertNotIn("power_on_hours", row)

    def test_a_host_forgotten_and_read_again_has_no_old_reading(self):
        SMART.keep_last("node-1", {"sda": dict(AWAKE)})
        SMART.forget("node-1")
        row = SMART.keep_last("node-1", {"sda": self.asleep()})["sda"]
        self.assertNotIn("power_on_hours", row)

    def test_a_restart_finds_the_kept_reading_on_the_data_volume(self):
        SMART.keep_last("node-1", {"sda": dict(AWAKE)})
        SMART.reset()            # memory only: a restart, or another copy taking over
        row = SMART.keep_last("node-1", {"sda": self.asleep()})["sda"]
        self.assertEqual((True, 1200), (row["asleep"], row["power_on_hours"]))

    def test_each_host_keeps_its_own_entry_on_the_data_volume(self):
        SMART.keep_last("node-1", {"sda": dict(AWAKE)})
        SMART.reset()
        SMART.keep_last("node-2", {"sda": dict(AWAKE, power_on_hours=5)})
        SMART.reset()
        self.assertEqual(1200, SMART.keep_last("node-1", {"sda": self.asleep()})["sda"]["power_on_hours"])
        self.assertEqual(5, SMART.keep_last("node-2", {"sda": self.asleep()})["sda"]["power_on_hours"])

    def test_forgetting_a_host_removes_it_from_the_data_volume(self):
        SMART.keep_last("node-1", {"sda": dict(AWAKE)})
        SMART.forget("node-1")
        SMART.reset()
        row = SMART.keep_last("node-1", {"sda": self.asleep()})["sda"]
        self.assertNotIn("power_on_hours", row)

    def test_a_data_volume_that_cannot_be_written_does_not_stop_the_reading(self):
        SMART.SHARED.DIR = "/nonexistent/smart-store"
        row = SMART.keep_last("node-1", {"sda": dict(AWAKE)})["sda"]
        self.assertEqual(1200, row["power_on_hours"])

    def test_a_drive_read_by_name_around_a_self_test_is_kept_too(self):
        SMART.keep_last("node-1", {"sda": dict(AWAKE), "sdb": dict(AWAKE, name="sdb")})
        fresh = dict(AWAKE, power_on_hours=1300)
        with mock.patch.object(SMART, "_request", return_value={"disk": fresh}):
            self.assertEqual(1300, SMART.disk("node-1", "sda")["power_on_hours"])
        rows = SMART.keep_last("node-1", {"sda": self.asleep(), "sdb": self.asleep("sdb")})
        self.assertEqual(1300, rows["sda"]["power_on_hours"], "the by-name reading was not kept")
        self.assertEqual(1200, rows["sdb"]["power_on_hours"], "a by-name reading dropped the other drives")

    def test_the_pages_own_read_keeps_the_last_reading_too(self):
        SMART.keep_last("node-1", {"sda": dict(AWAKE)})
        with mock.patch.object(SMART, "inventory", return_value={"disks": [self.asleep()]}):
            row = SMART.latest_disk("node-1", "sda")
        self.assertEqual((True, 1200), (row["asleep"], row["power_on_hours"]))


class ProbeLoopStandbyTests(Base):
    def setUp(self):
        super().setUp()
        PROBES.stop()
        PROBES.reset()
        self.addCleanup(PROBES.reset)
        self.pods = probe_pods
        patch = mock.patch.object(PROBES.NAMES, "kget", lambda path: self.pods(path), create=True)
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_loop_keeps_a_sleeping_drives_last_reading(self):
        PROBES.run_round(now=1000, smart=True)
        first = ROWS[0]
        ROWS[0] = {"name": "sda", "available": True, "asleep": True, "health": "unknown"}
        self.addCleanup(ROWS.__setitem__, 0, first)
        PROBES.run_round(now=1000 + PROBES.SMART_EVERY, smart=True)
        row = PROBES._state["node-1"]["smart"]["sda"]
        self.assertTrue(row["asleep"])
        self.assertEqual("PASSED", row["health"])
        self.assertTrue(SMART.latest_disk("node-1", "sda")["asleep"])

    def test_a_host_whose_probe_did_not_answer_is_not_asked_for_smart(self):
        PROBES.run_round(now=1000, smart=True)
        self.assertEqual(1, self.helper.calls)
        real = self.helper.urlopen

        def down(request, timeout=None):
            if ":9099/" in getattr(request, "full_url", request):
                raise OSError("timed out")
            return real(request, timeout)
        with mock.patch.object(SMART.urllib.request, "urlopen", down):
            PROBES.run_round(now=1000 + PROBES.SMART_EVERY, smart=True)
        self.assertEqual(1, self.helper.calls, "SMART was read on a host whose probe had just failed")
        self.assertFalse(PROBES._state["node-1"]["smart_stale"])
        # Back up: it is still due, and read at once.
        PROBES.run_round(now=1000 + PROBES.SMART_EVERY + PROBES.INTERVAL)
        self.assertEqual(2, self.helper.calls)

    def test_a_host_whose_temperature_read_fails_but_answers_is_still_read_for_smart(self):
        real = self.helper.urlopen

        def broken(request, timeout=None):
            if ":9099/" in getattr(request, "full_url", request):
                return _Response(b"not json")
            return real(request, timeout)
        with mock.patch.object(SMART.urllib.request, "urlopen", broken):
            PROBES.run_round(now=1000, smart=True)
        self.assertEqual(1, self.helper.calls, "a host that answered was not read for SMART")
        self.assertIn("sda", PROBES._state["node-1"]["smart"])


class AsleepAlertTests(Base):
    """A drive asleep with no reading kept (after a restart) proves nothing."""

    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        ALERTS.bind(folder.name)
        self.clock = 1_000_000.0

    def tick(self, overview, seconds=60):
        self.clock += seconds
        return ALERTS.observe({"health": ALERTS.health_facts(overview), "jobs": []}, self.clock)

    def overview(self, smart, issues=()):
        return {"health_issues": list(issues),
                "nodes": [{"name": "node-1", "temps": {"disks": [{"name": "sda", "smart": smart}]}}]}

    def test_an_open_drive_alert_stays_open_while_the_drive_sleeps_unread(self):
        failing = [{"severity": "critical", "kind": "Disk", "name": "node-1/sda", "reason": "4 pending sectors"}]
        bad = SMART.keep_last("node-1", {"sda": dict(AWAKE, pending=4)})["sda"]
        self.tick(self.overview(bad, failing))
        opened = self.tick(self.overview(bad, failing), seconds=90)
        self.assertEqual(["Drive node-1/sda needs attention"], [a["title"] for a in opened])
        SMART.reset()            # Homestead restarted on a data volume with no reading kept
        SMART.SHARED.DIR = tempfile.mkdtemp(dir=self.store)
        row = SMART.keep_last("node-1", {"sda": {"name": "sda", "available": True, "asleep": True}})["sda"]
        self.assertEqual("asleep", NODES.smart_disk_health(row, CFG)["state"])
        for _ in range(5):
            self.assertEqual([], self.tick(self.overview(row)), "the alert cleared on no reading")
        self.assertEqual(1, len(ALERTS.active()))


if __name__ == "__main__":
    unittest.main()
