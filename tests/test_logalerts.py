"""Log alerts: a saved log search that raises an alert when it matches too
many lines (spike 12, built). The alert carries counts, never a line and
never the query: alert text reaches lock screens and config backups."""
import calendar
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import homestead_logalerts as LA
import homestead_logsearch as LOGSEARCH

SECRET = "token=s3cr3t-value"


def workload(name, *pods, ns="lab"):
    return {"ns": ns, "name": name, "pods": [{"name": p, "phase": "Running", "containers": [{"name": name}]}
                                              for p in pods or (f"{name}-1",)]}


WL = [workload("app-x"), workload("app-y"), workload("app-z")]
STAMP = "2026-10-23T10:{:02d}:{:02d}.{:09d}Z"
T0 = calendar.timegm((2026, 10, 23, 10, 1, 2, 0, 0, 0))   # a round at 10:01:02, after the lines of 10:00
M0 = (T0 - 62) // 60                                       # the minute 10:00


def lines(n, minute=0, text="ERROR {} {}".format(SECRET, "{}")):
    return "\n".join(STAMP.format(minute, i % 60, i) + " " + text.format(i) for i in range(n))


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.logs, self.calls, self.leader = {}, [], [True]
        self.workloads = list(WL)
        LA.bind(self.dir, workloads=lambda: self.workloads, read=self.read, is_leader=lambda: self.leader[0])
        LA.reset()

    def read(self, ns, pod, container, since, limit):
        self.calls.append((pod, since))
        value = self.logs.get(pod, "")
        if isinstance(value, Exception):
            raise value
        return value[:limit]

    def rule(self, **extra):
        body = {"name": "App X trouble", "apps": ["lab/app-x"], "query": "ERROR", "threshold": 10, "window": 1}
        body.update(extra)
        return LA.save(body)

    def stored(self):
        with open(os.path.join(self.dir, "logalerts.json"), encoding="utf-8") as handle:
            return handle.read()


class Rules(Base):
    def test_ids_are_made_by_the_server(self):
        rule = self.rule()
        self.assertRegex(rule["id"], r"^la-[0-9a-f]{12}$")
        self.assertNotEqual(rule["id"], self.rule(name="Another")["id"])

    def test_client_id_must_name_an_existing_rule(self):
        for bad in ("../../etc", "la-000000000000", "la-ZZZ", 7, ""):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "no longer exists"):
                self.rule(id=bad)
        first = self.rule()
        again = self.rule(id=first["id"], threshold=20)
        self.assertEqual(first["id"], again["id"])
        self.assertEqual([20], [r["threshold"] for r in LA.listing()["rules"]])

    def test_name_may_not_repeat_the_query(self):
        with self.assertRaisesRegex(ValueError, "may not repeat the search text"):
            self.rule(name="frigate TOKEN=abc", query="token=abc")
        self.assertEqual("Front door errors", self.rule(name="Front door errors", query="Unable to read")["name"])
        self.assertEqual("App x errors", self.rule(name="App x errors", query="x")["name"], "a query this short is no secret")

    def test_names_are_required_and_short(self):
        for name in ("", "  ", "n" * 61):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "name"):
                self.rule(name=name)

    def test_rules_must_name_one_to_five_running_apps(self):
        with self.assertRaisesRegex(ValueError, "choose the app"):
            self.rule(apps=[])
        with self.assertRaisesRegex(ValueError, "at most 5 apps"):
            self.rule(apps=[f"lab/a{i}" for i in range(6)])
        with self.assertRaisesRegex(ValueError, "not running here"):
            self.rule(apps=["lab/elsewhere"])

    def test_bad_queries_are_refused_as_search_refuses_them(self):
        with self.assertRaisesRegex(ValueError, "take ages"):
            self.rule(query="(a+)+", regex=True)
        with self.assertRaisesRegex(ValueError, "not a valid regular expression"):
            self.rule(query="(", regex=True)

    def test_thresholds_and_windows(self):
        for extra in ({"threshold": 0}, {"threshold": "x"}, {"window": 2}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.rule(**extra)

    def test_at_most_twenty_rules(self):
        for i in range(LA.MAX_RULES):
            self.rule(name=f"Rule {i}")
        with self.assertRaisesRegex(ValueError, "at most 20"):
            self.rule(name="One more")

    def test_delete_takes_only_an_existing_id(self):
        rule = self.rule()
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            LA.delete("la-../x")
        LA.delete(rule["id"])
        self.assertEqual([], LA.listing()["rules"])

    def test_edit_that_changes_what_is_counted_resets_counts(self):
        rule = self.rule()
        self.logs["app-x-1"] = lines(3)
        LA.evaluate(now=T0)
        self.rule(id=rule["id"], threshold=5)
        self.assertEqual([[M0, 3], [M0 + 1, 0]], LA.listing()["rules"][0]["counts"], "a threshold change keeps them")
        self.rule(id=rule["id"], query="WARN")
        self.assertEqual([], LA.listing()["rules"][0]["counts"], "a new query starts again")


class Rounds(Base):
    def test_over_threshold_raises_a_fact_without_lines_or_query(self):
        rule = self.rule(query="ERROR")
        self.logs["app-x-1"] = lines(12)
        facts = LA.evaluate(now=T0)
        self.assertEqual(1, len(facts))
        fact = facts[0]
        self.assertEqual((f"logs:{rule['id']}", "logs"), (fact["key"], fact["category"]))
        self.assertIn("12 matching lines in a minute", fact["title"])
        blob = json.dumps(facts) + self.stored()
        self.assertNotIn("s3cr3t", blob)
        self.assertNotIn("ERROR", json.dumps(facts), "nor the query")
        self.assertEqual("/containers?panel=logalerts", fact["href"])

    def test_under_threshold_raises_nothing(self):
        self.rule()
        self.logs["app-x-1"] = lines(10)
        self.assertEqual([], LA.evaluate(now=T0))

    def test_window_sums_recent_minutes(self):
        self.rule(threshold=10, window=5)
        for minute in range(3):
            self.logs["app-x-1"] = lines(4, minute=minute)
            facts = LA.evaluate(now=T0 + 60 * minute)
        self.assertIn("12 matching lines in 5 minutes", facts[0]["title"])

    def test_unreadable_app_is_unknown_not_zero(self):
        rule = self.rule()
        self.logs["app-x-1"] = OSError("pod is being replaced")
        self.assertEqual([{"unknown_key": f"logs:{rule['id']}"}], LA.evaluate(now=T0))
        row = LA.listing()["rules"][0]
        self.assertEqual([[M0 + 1, None]], row["counts"])
        self.assertEqual("unread", row["status"]["state"])
        self.assertIn("could not read app-x", row["status"]["detail"])

    def test_reads_beyond_the_minute_limit_are_named(self):
        first = self.rule()
        second = self.rule(name="App Y trouble", apps=["lab/app-y"])
        self.logs["app-x-1"] = self.logs["app-y-1"] = lines(12)
        with mock.patch.object(LA, "MAX_CONTAINERS", 1):
            facts = LA.evaluate(now=T0)
        unknown = [f["unknown_key"][5:] for f in facts if "unknown_key" in f]
        raised = [f["key"][5:] for f in facts if "key" in f]
        self.assertEqual(1, len(unknown))
        self.assertEqual({first["id"], second["id"]}, set(unknown + raised))
        status = {r["id"]: r["status"] for r in LA.listing()["rules"]}[unknown[0]]
        self.assertEqual("skipped", status["state"])
        self.assertIn("containers a minute", status["detail"])

    def test_slow_expression_times_out_and_other_rules_still_count(self):
        slow = self.rule(name="Slow one", query="x", regex=True)
        plain = self.rule(name="Plain one", apps=["lab/app-y"])
        self.logs["app-x-1"] = self.logs["app-y-1"] = lines(12)
        real = LA.matching

        def matching(found, query, regex, case, limit=LA.REGEX_SECONDS):
            if regex:
                raise LA.TooSlow("the expression took more than 2 seconds")
            return real(found, query, regex, case, limit)
        with mock.patch.object(LA, "matching", matching):
            facts = LA.evaluate(now=T0)
        self.assertIn({"unknown_key": f"logs:{slow['id']}"}, facts)
        self.assertTrue(any(f.get("key") == f"logs:{plain['id']}" for f in facts))
        status = {r["id"]: r["status"] for r in LA.listing()["rules"]}[slow["id"]]
        self.assertEqual("slow", status["state"])

    def test_cursor_drops_repeats_and_forgets_gone_pods(self):
        self.rule()
        self.logs["app-x-1"] = lines(12)
        self.assertEqual(1, len(LA.evaluate(now=T0)))
        self.assertEqual([], LA.evaluate(now=T0 + 60), "the same lines again are not counted twice")
        self.workloads = [workload("app-x", "app-x-2")]
        self.logs["app-x-2"] = lines(12, minute=2)
        self.assertEqual(1, len(LA.evaluate(now=T0 + 120)), "the replacement pod is read")
        self.assertNotIn(("lab", "app-x-1", "app-x"), LA._cursors)

    def test_late_round_reads_since_the_cursor(self):
        self.rule()
        self.logs["app-x-1"] = lines(1)
        LA.evaluate(now=T0)
        cursor_at = LA._cursors[("lab", "app-x-1", "app-x")][1]
        LA.evaluate(now=cursor_at + 180)
        self.assertEqual(185, self.calls[-1][1])
        LA.evaluate(now=cursor_at + 3600)
        self.assertEqual(300, self.calls[-1][1], "at most five minutes back")

    def test_round_keeps_edits_made_meanwhile(self):
        rule = self.rule()
        self.logs["app-x-1"] = lines(3)

        def read(*args):
            LA.delete(rule["id"])
            other = self.rule(name="Made meanwhile")
            self.made = other["id"]
            return lines(3)
        LA.bind(self.dir, workloads=lambda: self.workloads, read=read, is_leader=lambda: True)
        LA.evaluate(now=T0)
        rows = LA.listing()["rules"]
        self.assertEqual([self.made], [r["id"] for r in rows])

    def test_standby_reads_nothing(self):
        self.rule()
        self.logs["app-x-1"] = lines(12)
        LA.evaluate(now=T0)
        self.leader[0] = False
        calls = len(self.calls)
        LA.step(now=T0 + 60)
        self.assertEqual(calls, len(self.calls))
        self.assertEqual({}, LA._cursors, "a new term starts from a fresh read")

    def test_stale_facts_are_none(self):
        self.rule()
        self.logs["app-x-1"] = lines(12)
        LA.step(now=T0)
        self.assertEqual(1, len(LA.alert_facts(now=T0 + 100)))
        self.assertIsNone(LA.alert_facts(now=T0 + LA.STALE + 1))
        LA.reset()
        self.assertIsNone(LA.alert_facts(now=T0), "nothing evaluated yet: could not look")

    def test_alert_loop_does_not_read(self):
        self.rule()
        LA.step(now=T0)
        calls = len(self.calls)
        for _ in range(5):
            LA.alert_facts(now=T0 + 10)
        self.assertEqual(calls, len(self.calls))

    def test_reads_are_bounded(self):
        self.rule(apps=["lab/app-x"])
        LA.evaluate(now=T0)
        self.assertEqual([("app-x-1", LA.EVERY)], self.calls)
        self.assertEqual(256 * 1024, LA.MAX_BYTES)


class ReviewFixes(Base):
    """The first review's findings, each pinned."""

    def stamped(self, minute, second, n, start=0):
        return "\n".join(f"2026-10-23T10:{minute:02d}:{second:02d}.{start + i:09d}Z ERROR {i}" for i in range(n))

    def test_a_one_minute_burst_is_raised_on_the_alert_loop_s_first_look(self):
        import homestead_alerts as ALERTS
        ALERTS.bind(self.dir)
        self.rule()
        self.logs["app-x-1"] = lines(12)
        facts = LA.evaluate(now=T0)
        raised = ALERTS.observe({"logs": facts}, now=T0 + 5)
        self.assertEqual(["raised"], [e["phase"] for e in raised],
                         "the counting window is the debounce: no further 60 s hold the next round would cut short")

    def test_lines_count_in_the_minute_they_were_written(self):
        self.rule(threshold=10, window=1)
        self.logs["app-x-1"] = lines(1)
        LA.evaluate(now=T0)                       # cursor at 10:00
        self.logs["app-x-1"] = "\n".join(self.stamped(m, 5, 6, start=m * 100) for m in (1, 2, 3))
        facts = LA.evaluate(now=T0 + 180)         # a late round: three minutes at once
        self.assertEqual([], facts, "6 a minute for three minutes is not 18 in a minute")
        counts = dict(map(tuple, LA.listing()["rules"][0]["counts"]))
        self.assertEqual([6, 6, 6], [counts[M0 + m] for m in (1, 2, 3)])

    def test_the_minute_just_finished_is_checked_in_full(self):
        self.rule(threshold=10, window=1)
        self.logs["app-x-1"] = self.stamped(0, 30, 6)
        self.assertEqual([], LA.evaluate(now=T0 - 30))     # 10:00:32: six so far
        self.logs["app-x-1"] = self.stamped(0, 30, 6) + "\n" + self.stamped(0, 50, 6, start=100)
        facts = LA.evaluate(now=T0)                        # 10:01:02: 12 in 10:00
        self.assertIn("12 matching lines in a minute", facts[0]["title"])

    def test_reading_the_same_lines_twice_counts_them_once(self):
        self.rule()
        self.logs["app-x-1"] = lines(12)
        LA.evaluate(now=T0)
        LA.evaluate(now=T0 + 5)
        self.assertEqual(12, dict(map(tuple, LA.listing()["rules"][0]["counts"]))[M0])

    def test_an_app_with_nothing_running_is_not_zero(self):
        rule = self.rule()
        self.workloads = [workload("app-x", ns="lab")]
        self.workloads[0]["pods"] = []
        self.assertEqual([{"unknown_key": f"logs:{rule['id']}"}], LA.evaluate(now=T0))
        status = LA.listing()["rules"][0]["status"]
        self.assertEqual("absent", status["state"])
        self.assertIn("nothing of app-x is running", status["detail"])

    def test_an_edit_during_a_round_is_not_overwritten_by_old_counts(self):
        rule = self.rule()

        def read(*args):
            self.rule(id=rule["id"], query="WARN")
            return lines(12)
        LA.bind(self.dir, workloads=lambda: self.workloads, read=read, is_leader=lambda: True)
        self.assertEqual([], [f for f in LA.evaluate(now=T0) if "key" in f])
        self.assertEqual([], LA.listing()["rules"][0]["counts"])

    def test_a_rule_keeps_an_app_that_is_not_running_now(self):
        rule = self.rule()
        self.workloads = [workload("app-y")]
        self.assertEqual(20, self.rule(id=rule["id"], threshold=20)["threshold"])
        with self.assertRaisesRegex(ValueError, "not running here"):
            self.rule(name="New", apps=["lab/app-x"])

    def test_delete_with_a_body_that_is_not_an_object(self):
        route = LA.ROUTES[("POST", "/api/logalerts/delete")][1]
        for body in (None, [], "x", 3):
            with self.subTest(body=body), self.assertRaisesRegex(ValueError, "no longer exists"):
                route(mock.Mock(body=body))

    def test_ids_with_a_trailing_newline_are_refused(self):
        rule = self.rule()
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            LA.delete(rule["id"] + "\n")

    def test_the_list_says_when_counting_stopped(self):
        self.rule()
        self.logs["app-x-1"] = lines(3)
        LA.evaluate(now=T0)
        self.assertEqual(int(T0), LA.listing()["counted_at"])

    def test_skipped_rules_take_turns(self):
        first = self.rule()
        second = self.rule(name="App Y trouble", apps=["lab/app-y"])
        skipped = []
        with mock.patch.object(LA, "MAX_CONTAINERS", 1):
            for i in range(2):
                LA.evaluate(now=T0 + 60 * i)
                skipped += [r["id"] for r in LA.listing()["rules"] if r["status"]["state"] == "skipped"]
        self.assertEqual({first["id"], second["id"]}, set(skipped))

    def test_rounds_start_two_seconds_into_each_minute(self):
        waits = []

        def sleep(seconds):
            waits.append(seconds)
            raise StopIteration
        for at in (T0, T0 + 28):
            with mock.patch.object(LA, "step"), self.assertRaises(StopIteration):
                LA.run(sleep=sleep, clock=lambda: at)
        self.assertEqual([60, 32], waits)


class PreviewBounds(Base):
    BODY = {"name": "", "apps": ["lab/app-x"], "query": "ERROR", "threshold": 3, "window": 1}

    def test_preview_reads_less_than_a_search_does(self):
        seen = []
        LA.bind(self.dir, workloads=lambda: self.workloads,
                read=lambda ns, pod, c, since, limit: seen.append(limit) or "", is_leader=lambda: True)
        LA.preview(dict(self.BODY))
        self.assertEqual([LA.PREVIEW_BYTES], seen)
        self.assertLess(LA.PREVIEW_BYTES, LOGSEARCH.MAX_BYTES)

    def test_preview_reads_containers_side_by_side(self):
        import threading
        self.workloads = [workload("app-x", "app-x-1", "app-x-2")]
        both, barrier = [], threading.Barrier(2, timeout=3)

        def read(*args):
            barrier.wait()
            both.append(1)
            return ""
        LA.bind(self.dir, workloads=lambda: self.workloads, read=read, is_leader=lambda: True)
        LA.preview(dict(self.BODY))
        self.assertEqual(2, len(both))

    def test_preview_needs_no_name(self):
        self.assertEqual(60, len(LA.preview(dict(self.BODY, name=""))["counts"]))
        self.assertEqual(60, len(LA.preview(dict(self.BODY, name="ERROR here"))["counts"]))

    def test_a_preview_out_of_time_says_why(self):
        with mock.patch.object(LA, "matching", side_effect=LA.TooSlow("x")):
            with self.assertRaisesRegex(ValueError, "too many lines, or too slow an expression"):
                LA.preview(dict(self.BODY, regex=True))


class Matching(unittest.TestCase):
    def test_plain_text_matching_spawns_nothing(self):
        with mock.patch("subprocess.run", side_effect=AssertionError("spawned")):
            self.assertEqual([0, 2], LA.matching(["an Error", "fine", "error"], "error", False, False))
            self.assertEqual([2], LA.matching(["an Error", "fine", "error"], "error", False, True))

    def test_an_expression_is_matched_in_a_child_process(self):
        self.assertEqual([0, 2], LA.matching(["a 500 x", "a 200 x", "b 503"], r" 5\d\d", True, False))

    def test_matching_stops_a_runaway_expression(self):
        # Passes log search's nested-repeat check, yet backtracks for ages.
        LOGSEARCH.matcher(r"^(x?){40}x{40}$", True)
        started = time.monotonic()
        with self.assertRaises(LA.TooSlow):
            LA.matching(["x" * 40], r"^(x?){40}x{40}$", True, False, limit=0.5)
        self.assertLess(time.monotonic() - started, 5)


class Preview(Base):
    def test_preview_shares_the_log_search_lock(self):
        with LOGSEARCH.exclusive():
            with self.assertRaisesRegex(ValueError, "another log search is running"):
                LA.preview({"name": "x", "apps": ["lab/app-x"], "query": "ERROR", "threshold": 1, "window": 1})

    def test_preview_counts_per_minute_and_minutes_over(self):
        now = time.time()
        minute = int(now // 60)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime((minute - 2) * 60 + 5))
        self.logs["app-x-1"] = "\n".join(f"{stamp}.{i:09d}Z ERROR {SECRET}" for i in range(4))
        out = LA.preview({"name": "x", "apps": ["lab/app-x"], "query": "ERROR", "threshold": 3, "window": 1}, now=now)
        self.assertEqual(60, len(out["counts"]))
        self.assertEqual(4, dict(map(tuple, out["counts"]))[minute - 2])
        self.assertEqual(1, out["over_minutes"])
        self.assertNotIn("s3cr3t", json.dumps(out))
        self.assertEqual([], out["unread"])

    def test_preview_names_what_it_could_not_read(self):
        self.logs["app-x-1"] = OSError("gone")
        out = LA.preview({"name": "x", "apps": ["lab/app-x"], "query": "ERROR", "threshold": 3, "window": 1})
        self.assertEqual(["app-x"], out["unread"])

    def test_preview_takes_no_id(self):
        out = LA.preview({"id": "../x", "name": "x", "apps": ["lab/app-x"], "query": "ERROR", "threshold": 3, "window": 1})
        self.assertNotIn("rule", out)


class Wiring(unittest.TestCase):
    def test_routes_and_roles(self):
        import homestead_route_policy as POLICY
        import homestead_routes as ROUTES
        self.assertIn("homestead_logalerts", ROUTES.MODULES)
        self.assertEqual("viewer", POLICY.role("/api/logalerts", "GET"))
        self.assertEqual("viewer", POLICY.role("/api/logalerts/preview", "POST"))
        self.assertEqual("operator", POLICY.role("/api/logalerts", "POST"))
        self.assertEqual("operator", POLICY.role("/api/logalerts/delete", "POST"))

    def test_push_category_and_store_listed(self):
        import homestead_housekeeping as HOUSEKEEPING
        import homestead_push as PUSH
        self.assertIn("never the lines", PUSH.CATEGORIES["logs"])
        self.assertNotIn("logs", PUSH.DEFAULT_CATEGORIES)
        self.assertTrue(any("logalerts.json" in files for _, files, _ in HOUSEKEEPING.STORES))

    def test_the_alert_loop_takes_the_last_facts_and_starts_the_worker(self):
        path = os.path.join(os.path.dirname(__file__), "..", "server", "homestead_overview_server.py")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn('take("logs", LOGALERTS.alert_facts)', text)
        loop = text[text.index("def _alerts_loop"):]
        self.assertLess(loop.index("LOGALERTS.start()"), loop.index("while True"))


if __name__ == "__main__":
    unittest.main()
