"""Log alerts: a saved log search that raises an alert when it matches too
many lines (spike 12, built). The alert carries counts, never a line and
never the query: alert text reaches lock screens and config backups."""
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
        LA.evaluate(now=60_000)
        self.rule(id=rule["id"], threshold=5)
        self.assertEqual([[1000, 3]], LA.listing()["rules"][0]["counts"], "a threshold change keeps them")
        self.rule(id=rule["id"], query="WARN")
        self.assertEqual([], LA.listing()["rules"][0]["counts"], "a new query starts again")


class Rounds(Base):
    def test_over_threshold_raises_a_fact_without_lines_or_query(self):
        rule = self.rule(query="ERROR")
        self.logs["app-x-1"] = lines(12)
        facts = LA.evaluate(now=60_000)
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
        self.assertEqual([], LA.evaluate(now=60_000))

    def test_window_sums_recent_minutes(self):
        self.rule(threshold=10, window=5)
        for minute in range(3):
            self.logs["app-x-1"] = lines(4, minute=minute)
            facts = LA.evaluate(now=60_000 + 60 * minute)
        self.assertIn("12 matching lines in 5 minutes", facts[0]["title"])

    def test_unreadable_app_is_unknown_not_zero(self):
        rule = self.rule()
        self.logs["app-x-1"] = OSError("pod is being replaced")
        self.assertEqual([{"unknown_key": f"logs:{rule['id']}"}], LA.evaluate(now=60_000))
        row = LA.listing()["rules"][0]
        self.assertEqual([[1000, None]], row["counts"])
        self.assertEqual("unread", row["status"]["state"])
        self.assertIn("could not read app-x", row["status"]["detail"])

    def test_reads_beyond_the_minute_limit_are_named(self):
        first = self.rule()
        second = self.rule(name="App Y trouble", apps=["lab/app-y"])
        self.logs["app-x-1"] = self.logs["app-y-1"] = lines(12)
        with mock.patch.object(LA, "MAX_CONTAINERS", 1):
            facts = LA.evaluate(now=60_000)
        self.assertIn({"unknown_key": f"logs:{second['id']}"}, facts)
        self.assertTrue(any(f.get("key") == f"logs:{first['id']}" for f in facts))
        status = {r["id"]: r["status"] for r in LA.listing()["rules"]}[second["id"]]
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
            facts = LA.evaluate(now=60_000)
        self.assertIn({"unknown_key": f"logs:{slow['id']}"}, facts)
        self.assertTrue(any(f.get("key") == f"logs:{plain['id']}" for f in facts))
        status = {r["id"]: r["status"] for r in LA.listing()["rules"]}[slow["id"]]
        self.assertEqual("slow", status["state"])

    def test_cursor_drops_repeats_and_forgets_gone_pods(self):
        self.rule()
        self.logs["app-x-1"] = lines(12)
        self.assertEqual(1, len(LA.evaluate(now=60_000)))
        self.assertEqual([], LA.evaluate(now=60_060), "the same lines again are not counted twice")
        self.workloads = [workload("app-x", "app-x-2")]
        self.logs["app-x-2"] = lines(12, minute=2)
        self.assertEqual(1, len(LA.evaluate(now=60_120)), "the replacement pod is read")
        self.assertNotIn(("lab", "app-x-1", "app-x"), LA._cursors)

    def test_late_round_reads_since_the_cursor(self):
        self.rule()
        self.logs["app-x-1"] = lines(1)
        LA.evaluate(now=60_000)
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
        LA.evaluate(now=60_000)
        rows = LA.listing()["rules"]
        self.assertEqual([self.made], [r["id"] for r in rows])

    def test_standby_reads_nothing(self):
        self.rule()
        self.logs["app-x-1"] = lines(12)
        LA.evaluate(now=60_000)
        self.leader[0] = False
        calls = len(self.calls)
        LA.step(now=60_060)
        self.assertEqual(calls, len(self.calls))
        self.assertEqual({}, LA._cursors, "a new term starts from a fresh read")

    def test_stale_facts_are_none(self):
        self.rule()
        self.logs["app-x-1"] = lines(12)
        LA.step(now=60_000)
        self.assertEqual(1, len(LA.alert_facts(now=60_100)))
        self.assertIsNone(LA.alert_facts(now=60_000 + LA.STALE + 1))
        LA.reset()
        self.assertIsNone(LA.alert_facts(now=60_000), "nothing evaluated yet: could not look")

    def test_alert_loop_does_not_read(self):
        self.rule()
        LA.step(now=60_000)
        calls = len(self.calls)
        for _ in range(5):
            LA.alert_facts(now=60_010)
        self.assertEqual(calls, len(self.calls))

    def test_reads_are_bounded(self):
        self.rule(apps=["lab/app-x"])
        LA.evaluate(now=60_000)
        self.assertEqual([("app-x-1", LA.EVERY)], self.calls)
        self.assertEqual(256 * 1024, LA.MAX_BYTES)


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
