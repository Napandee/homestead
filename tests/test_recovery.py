"""Moving containers back after a host returns: noticing it, saying so, and
in the maintenance window doing it - but not under an app's backup."""
import calendar
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_recovery as recovery
import server

WINDOW = {"days": [0, 1, 2, 3, 4, 5, 6], "start": "02:00", "duration_minutes": 120}
# Wednesday 2026-10-07, at the given UTC time.
at = lambda h, m=0: calendar.timegm((2026, 10, 7, h, m, 0))


def node(name, ready=True, cordoned=False):
    return {"metadata": {"name": name}, "spec": {"unschedulable": cordoned},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


def app(name, claim):
    return {"metadata": {"name": name, "namespace": "lab"},
            "spec": {"template": {"spec": {"volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": claim}}]}}}}


def cronjob(name, schedule, claim, active=False, suspend=False, tz=""):
    spec = {"schedule": schedule, "suspend": suspend,
            "jobTemplate": {"spec": {"template": {"spec": {"volumes": [{"name": "d", "persistentVolumeClaim": {"claimName": claim}}]}}}}}
    if tz:
        spec["timeZone"] = tz
    return {"metadata": {"name": name, "namespace": "lab"}, "spec": spec, "status": {"active": [{}] if active else []}}


class TempData(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        recovery.bind(folder.name)
        self.addCleanup(recovery.bind, "/data")


class WindowTests(unittest.TestCase):
    def test_the_window_is_open_inside_it_and_says_when_it_closes(self):
        self.assertEqual((True, at(4)), recovery.window(WINDOW, at(2, 30)))
        self.assertEqual((False, 0), recovery.window(WINDOW, at(4)))

    def test_a_window_across_midnight_closes_on_the_next_day(self):
        late = {**WINDOW, "start": "23:00"}
        self.assertEqual((True, at(1)), recovery.window(late, at(0, 30)))

    def test_each_opening_is_its_own(self):
        self.assertEqual(recovery.window_key(WINDOW, at(2)), recovery.window_key(WINDOW, at(3, 59)))
        self.assertNotEqual(recovery.window_key(WINDOW, at(2)), recovery.window_key(WINDOW, at(2) + 86400))
        self.assertEqual("", recovery.window_key(WINDOW, at(12)))


class CronTests(unittest.TestCase):
    def test_a_nightly_job_fires_inside_the_window_and_not_outside(self):
        self.assertTrue(recovery.fires_between("15 2 * * *", at(2), at(4)))
        self.assertFalse(recovery.fires_between("15 5 * * *", at(2), at(4)))
        self.assertTrue(recovery.fires_between("@daily", at(23) - 86400, at(1)))

    def test_days_of_the_week_and_steps(self):
        self.assertFalse(recovery.fires_between("30 2 * * 1", at(2), at(4)))      # Mondays; this is a Wednesday
        self.assertTrue(recovery.fires_between("30 2 * * 3", at(2), at(4)))
        self.assertTrue(recovery.fires_between("*/20 * * * *", at(2, 1), at(2, 21)))

    def test_a_job_in_its_own_time_zone(self):
        # 03:15 in London is 02:15 UTC in October (BST).
        self.assertTrue(recovery.fires_between("15 3 * * *", at(2), at(2, 30), "Europe/London"))
        self.assertFalse(recovery.fires_between("15 3 * * *", at(2, 30), at(4), "Europe/London"))

    def test_an_unreadable_schedule_is_not_known(self):
        self.assertIsNone(recovery.fires_between("every night", at(2), at(4)))
        self.assertIsNone(recovery.fires_between("0 2 * * *", at(2), at(4), "Mars/Olympus"))


class GuardTests(unittest.TestCase):
    def test_an_app_whose_backup_runs_in_the_window_stays(self):
        kept = recovery.guarded([cronjob("backup-web", "15 2 * * *", "web-data")],
                                [app("web", "web-data"), app("db", "db-data")], at(2), at(4))
        self.assertEqual({"lab/web"}, set(kept))
        self.assertIn("backup-web", kept["lab/web"])

    def test_a_running_or_unreadable_job_keeps_its_app_and_a_paused_one_does_not(self):
        deps = [app("web", "web-data")]
        self.assertIn("lab/web", recovery.guarded([cronjob("b", "0 12 * * *", "web-data", active=True)], deps, at(2), at(4)))
        self.assertIn("lab/web", recovery.guarded([cronjob("b", "nonsense", "web-data")], deps, at(2), at(4)))
        self.assertFalse(recovery.guarded([cronjob("b", "15 2 * * *", "web-data", suspend=True)], deps, at(2), at(4)))


class HostTests(TempData):
    def test_a_host_seen_for_the_first_time_has_not_come_back(self):
        self.assertEqual({}, recovery.observe([node("a"), node("b")], at(1)))

    def test_a_host_ready_again_or_uncordoned_has_come_back(self):
        recovery.observe([node("a"), node("b", ready=False), node("c", cordoned=True)], at(1))
        self.assertEqual({"b", "c"}, set(recovery.observe([node("a"), node("b"), node("c")], at(1, 5))))

    def test_a_host_down_again_or_a_day_later_is_forgotten(self):
        recovery.observe([node("b", ready=False)], at(1))
        recovery.observe([node("b")], at(1, 5))
        self.assertEqual({}, recovery.observe([node("b", ready=False)], at(1, 10)))
        recovery.observe([node("b")], at(1, 15))
        self.assertEqual({}, recovery.observe([node("b")], at(1, 15) + recovery.RETURN_KEEP))


class AlertTests(TempData):
    def back(self, moves):
        recovery.observe([node("b", ready=False)], at(1))
        recovery.observe([node("b")], at(1, 5))
        recovery.suggest([{}] * moves, at(1, 5))

    def test_a_host_back_with_containers_to_move_raises_an_info_alert(self):
        self.back(3)
        fact = recovery.alert_facts("suggest")[0]
        self.assertEqual(("joins", "info", "/containers"), (fact["category"], fact["severity"], fact["href"]))
        self.assertIn("b is back: 3 containers", fact["title"])
        self.assertIn("Nothing moves until you review it", fact["body"])

    def test_in_window_mode_it_says_when_they_move(self):
        self.back(1)
        self.assertIn("next maintenance window (02:00 UTC)", recovery.alert_facts("window", WINDOW)[0]["body"])

    def test_nothing_to_move_forgets_the_host_and_off_says_nothing(self):
        self.back(2)
        self.assertEqual([], recovery.alert_facts("off"))
        recovery.suggest([], at(1, 10))
        self.assertEqual([], recovery.alert_facts("suggest"))
        self.assertEqual({}, recovery.observe_returned())


class TickTests(TempData):
    """One pass of the leader's loop, against a fake cluster."""
    def setUp(self):
        super().setUp()
        self.nodes = [node("a"), node("b", ready=False)]
        self.settings = {"placement": {"rebalance": "window"}, "updates": {"maintenance": WINDOW}}
        self.plans = []
        api = {"/apis/batch/v1/cronjobs": {"items": [cronjob("backup-web", "15 2 * * *", "web-data")]},
               "/apis/apps/v1/deployments": {"items": [app("web", "web-data"), app("db", "db-data")]}}
        self.avoided = []
        def plan(exclude=(), avoid=()):
            self.plans.append(sorted(exclude))
            self.avoided.append(sorted(avoid))
            return {"moves": [{"id": "lab/db"}], "excluded": sorted(exclude)}
        for patcher in [mock.patch.object(server, "kget", side_effect=lambda p: {"items": self.nodes} if p == "/api/v1/nodes" else api[p]),
                        mock.patch.object(server, "cached", side_effect=lambda key, ttl, fn: self.settings),
                        mock.patch.object(server.OPS, "list_operations", return_value=[]),
                        mock.patch.object(server.CREBALANCE, "plan", side_effect=plan),
                        mock.patch.object(server, "start_container_rebalance", return_value={"id": "job"}),
                        mock.patch.object(server, "_recovery_planned", [0.0])]:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_in_the_window_it_rebalances_once_leaving_apps_whose_backup_is_due(self):
        server.recovery_tick(at(1))
        self.nodes = [node("a"), node("b")]
        self.assertIsNone(server.recovery_tick(at(1, 30)))            # back, but outside the window
        self.assertEqual({"id": "job"}, server.recovery_tick(at(2, 5)))
        self.assertEqual(["lab/web"], self.plans[-1])
        server.start_container_rebalance.assert_called_once()
        self.assertIn("after b came back", server.start_container_rebalance.call_args[0][1])
        self.assertIsNone(server.recovery_tick(at(2, 30)))            # once per opening

    def test_suggest_mode_never_moves_anything_itself(self):
        self.settings["placement"]["rebalance"] = "suggest"
        server.recovery_tick(at(1))
        self.nodes = [node("a"), node("b")]
        server.recovery_tick(at(2, 5))
        server.start_container_rebalance.assert_not_called()
        self.assertTrue(recovery.alert_facts("suggest"))

    def test_a_rebalance_already_running_is_left_to_finish(self):
        server.recovery_tick(at(1))
        self.nodes = [node("a"), node("b")]
        with mock.patch.object(server.OPS, "list_operations", return_value=[{"kind": server.CREBALANCE.KIND, "status": "running"}]):
            self.assertIsNone(server.recovery_tick(at(2, 5)))
        server.start_container_rebalance.assert_not_called()


class SettingsTests(unittest.TestCase):
    def test_the_setting_defaults_to_off_and_takes_only_its_modes(self):
        self.assertEqual("off", server.validate_app_settings({})["placement"]["rebalance"])
        self.assertEqual("window", server.validate_app_settings({"placement": {"rebalance": "window"}})["placement"]["rebalance"])
        with self.assertRaisesRegex(ValueError, "off, suggest or window"):
            server.validate_app_settings({"placement": {"rebalance": "always"}})


if __name__ == "__main__":
    unittest.main()
