"""The audit line (#738): one per change request, saying who made it, how they
came in, what, the target, the status and how long it took - and nothing a
request carried that was secret."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import homestead_audit as AUDIT_LOG
import homestead_routes as ROUTES

KEY = {"id": "k1", "name": "ha", "owner": "ada", "scopes": ["apps:control"], "expires": None, "kind": "key"}


def view(method="POST", path="/api/workload/scale", body=None, user="ada", role="operator", addr="192.0.2.10",
         access=None, fleet=None, key=None, headers=()):
    """What PR 1's after_request hands a hook registered with fields=HOOK_FIELDS."""
    conn = ROUTES.Connection(ROUTES.Headers(list(headers)), {}, False, False, access, fleet, key, None)
    return ROUTES.Request(method, path, None, {} if body is None else body, user, role, addr, conn)


class TheLine(unittest.TestCase):
    def line(self, request, status=200, ms=142, notes=None):
        return AUDIT_LOG.line(request, status, ms, notes or {}, now=1792574043.512)

    def test_a_browser_line_has_exactly_its_fields(self):
        with mock.patch.dict("os.environ", {"HOSTNAME": "homestead-6d9f-abcde"}):
            line = self.line(view(body={"ns": "lab", "kind": "Deployment", "name": "app-1"}))
        self.assertEqual({"v": 1, "at": "2026-10-21T09:14:03.512Z", "replica": "homestead-6d9f-abcde",
                          "via": "browser", "user": "ada", "role": "operator", "addr": "192.0.2.10",
                          "method": "POST", "path": "/api/workload/scale",
                          "target": {"ns": "lab", "kind": "Deployment", "name": "app-1"},
                          "status": 200, "ms": 142}, line)

    def test_cloudflare_access_adds_the_verified_email_only(self):
        self.assertEqual("ada@example.com", self.line(view(access={"email": "ada@example.com", "sub": "x"}))["access"])
        for bad in ({"email": "a b@example.com"}, {"email": "no-at"}, {"email": 7}, {"email": "a@" + "b" * 260},
                    {}, None):
            with self.subTest(access=bad):
                self.assertNotIn("access", self.line(view(access=bad)))

    def test_an_api_key_says_which_key_and_whose(self):
        line = self.line(view(path="/api/v1/apps/restart", user="api-key:ha", key=KEY))
        self.assertEqual(("key", "api-key:ha", "k1", "ada"), (line["via"], line["user"], line["key"], line["owner"]))

    def test_mcp_needs_a_change_tool_note(self):
        request = view(path="/mcp", user="api-key:ha", key=KEY)
        self.assertIsNone(self.line(request))
        self.assertIsNone(self.line(request, notes={"tool": "list_apps", "kind": "read"}))
        line = self.line(request, notes={"tool": "restart_app", "kind": "change"})
        self.assertEqual(("mcp", "restart_app"), (line["via"], line["tool"]))
        self.assertNotIn("kind", line)

    def test_a_linked_homestead_says_where_from(self):
        line = self.line(view(user="fleet:loft:ada", role="operator", fleet="m1"))
        self.assertEqual(("linked", "fleet:loft:ada", "m1"), (line["via"], line["user"], line["from"]))

    def test_a_stranger_is_none(self):
        line = self.line(view(user=None, role=None), status=401)
        self.assertEqual(("none", "", None, 401), (line["via"], line["user"], line["role"], line["status"]))

    def test_no_body_key_but_the_named_four(self):
        text = json.dumps(self.line(view(body={"ns": "lab", "name": "app-1", "password": "Pw-0123456789",
                                               "env": "SECRET=1"})))
        self.assertNotIn("Pw-0123456789", text)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("password", text)

    def test_query_never_and_path_capped_and_one_line(self):
        text = AUDIT_LOG.text(self.line(view(path="/api/a\nb", headers=[("X-Q", "token=abc")])))
        self.assertNotIn("\n", text)
        self.assertEqual("/api/a\nb", json.loads(text)["path"])
        self.assertNotIn("abc", text)
        self.assertEqual(200, len(self.line(view(path="/api/" + "a" * 300))["path"]))

    def test_target_from_a_delete_path_and_bad_names_dropped(self):
        self.assertEqual({"ns": "lab", "name": "app-1"},
                         self.line(view("DELETE", "/api/workload/lab/app-1", body={}))["target"])
        self.assertEqual({"ns": "lab"}, self.line(view(body={"namespace": "lab", "name": "-x"}))["target"])
        for body in ({"name": "a b"}, {"name": "x" * 300}, {"ns": ""}):
            with self.subTest(body=body):
                self.assertNotIn("target", self.line(view(body=body)))

    def test_notes_are_checked_too(self):
        line = self.line(view(), notes={"to": "member-2", "held": "a b"})
        self.assertEqual("member-2", line["to"])
        self.assertNotIn("held", line)

    def test_consoles_carry_their_length(self):
        line = self.line(view("GET", "/api/node/shell", body={}), status=101, ms=65_400)
        self.assertEqual((101, 65), (line["status"], line["session_s"]))


class WhatIsAudited(unittest.TestCase):
    def test_changes_and_consoles(self):
        for method, path in (("POST", "/api/x"), ("PUT", "/api/x"), ("PATCH", "/api/x"), ("DELETE", "/api/x"),
                             ("POST", "/mcp"), ("GET", "/api/console"), ("GET", "/api/node/shell"),
                             ("GET", "/api/vm/console")):
            with self.subTest(method=method, path=path):
                self.assertTrue(AUDIT_LOG.audited(method, path))
        for method, path in (("GET", "/api/overview"), ("POST", "/index.html"), ("GET", "/mcp"),
                             ("POST", "/api/auth/login"), ("POST", "/api/auth/preferences/dashboard"),
                             ("HEAD", "/api/x"), ("", "")):
            with self.subTest(method=method, path=path):
                self.assertFalse(AUDIT_LOG.audited(method, path))

    def test_notes_refuse_unknown_keys(self):
        with self.assertRaises(ValueError):
            AUDIT_LOG.note(password="x")
        AUDIT_LOG._take_notes()


class Recording(unittest.TestCase):
    def setUp(self):
        self.out = []
        self.enterContext(mock.patch.object(AUDIT_LOG, "emit", side_effect=lambda entry, keep=True: self.out.append(entry)))
        AUDIT_LOG._buckets.clear()
        self.addCleanup(AUDIT_LOG._buckets.clear)

    def test_a_read_writes_nothing_and_a_change_one_line(self):
        AUDIT_LOG.record(view("GET", "/api/overview"), 200, 3)
        AUDIT_LOG.record(view(), 200, 3)
        self.assertEqual(["/api/workload/scale"], [line["path"] for line in self.out])

    def test_record_never_raises(self):
        with mock.patch.object(AUDIT_LOG, "emit", side_effect=OSError("disk")):
            self.assertIsNone(AUDIT_LOG.record(view(), 200, 1))
        with mock.patch.object(AUDIT_LOG, "line", side_effect=RuntimeError("bug")):
            self.assertIsNone(AUDIT_LOG.record(view(), 200, 1))


class Throttle(unittest.TestCase):
    """A flood of refused, unauthenticated or no-op requests cannot wash real
    changes out of audit.jsonl's two files: on the data volume they are kept a
    few a minute per address and per user, and summed up after; stdout still
    carries every line, and a signed-in person's successful change is always kept."""

    def setUp(self):
        self.clock = [1000.0]
        self.enterContext(mock.patch.object(AUDIT_LOG.time, "time", side_effect=lambda: self.clock[0]))
        self.enterContext(mock.patch.object(AUDIT_LOG, "_file_on", return_value=True))
        self.stdout, self.kept = [], []
        self.enterContext(mock.patch.object(AUDIT_LOG, "_stdout", side_effect=lambda t: self.stdout.append(json.loads(t))))
        self.enterContext(mock.patch.object(AUDIT_LOG, "_append", side_effect=lambda t: self.kept.append(json.loads(t))))
        AUDIT_LOG._buckets.clear()
        self.addCleanup(AUDIT_LOG._buckets.clear)
        AUDIT_LOG._swept[0] = 0

    def stranger(self, addr="192.0.2.7", status=401, user=None):
        AUDIT_LOG.record(view(user=user, role=None if user is None else "viewer", addr=addr), status, 1)

    def summaries(self):
        return [line for line in self.kept if "suppressed" in line]

    def test_signed_in_successes_are_never_throttled(self):
        for _ in range(500):
            AUDIT_LOG.record(view(), 200, 1)
        self.assertEqual(500, len(self.kept))
        self.assertEqual(500, len(self.stdout))

    def test_refusals_keep_a_few_per_address_then_a_summary(self):
        for _ in range(20):
            self.stranger()
        self.assertEqual(20, len(self.stdout), "stdout carries every line")
        self.assertEqual(AUDIT_LOG.BURST, len(self.kept))
        self.clock[0] += AUDIT_LOG.WINDOW_SECONDS + 1
        self.stranger()
        (summary,) = self.summaries()
        self.assertEqual({"suppressed": 20 - AUDIT_LOG.BURST, "addr": "192.0.2.7", "window_s": AUDIT_LOG.WINDOW_SECONDS},
                         {k: summary[k] for k in ("suppressed", "addr", "window_s")})
        self.assertIn("more refused requests from 192.0.2.7", summary["detail"])
        self.assertEqual(401, self.kept[-1]["status"], "a new minute keeps lines again")

    def test_a_summary_comes_even_when_the_flood_stops(self):
        for _ in range(9):
            self.stranger()
        self.clock[0] += AUDIT_LOG.WINDOW_SECONDS + AUDIT_LOG.SWEEP_SECONDS + 1
        AUDIT_LOG.record(view(), 200, 1)                     # anyone's next change
        self.assertEqual([9 - AUDIT_LOG.BURST], [line["suppressed"] for line in self.summaries()])

    def test_a_signed_in_users_refusals_are_throttled_across_addresses(self):
        for n in range(20):
            self.stranger(addr=f"198.51.100.{n}", status=403, user="node1")
        self.assertEqual(20, len(self.stdout))
        self.assertEqual(AUDIT_LOG.BURST, len([line for line in self.kept if line.get("status") == 403]))

    def test_only_refusals_are_throttled(self):
        """Successes, anonymous ones too (first-admin setup), and 5xx are always kept."""
        for _ in range(20):
            AUDIT_LOG.record(view("POST", "/api/auth/setup", user=None, role=None), 200, 1)
            self.stranger(status=500)
            self.stranger(status=None)
        self.assertEqual(60, len(self.kept))
        for _ in range(20):
            self.stranger(status=409, user="ada")
        self.assertEqual(60 + AUDIT_LOG.BURST, len(self.kept), "a 4xx refusal is throttled")

    def test_a_flood_from_one_address_suppresses_nothing_else(self):
        for _ in range(300):
            self.stranger(addr="192.0.2.7")
        kept_before = len(self.kept)
        self.stranger(addr="192.0.2.8", status=403, user="ada")             # (a) a signed-in refusal
        AUDIT_LOG.record(view("POST", "/api/auth/setup", user=None, role=None, addr="192.0.2.7"), 200, 1)  # (b)
        self.stranger(addr="192.0.2.7", status=500)                          # (c)
        self.stranger(addr="192.0.2.9")                                       # (d) a second address's first 401
        self.assertEqual([403, 200, 500, 401], [line["status"] for line in self.kept[kept_before:]])

    def test_many_addresses_each_keep_their_first_refusal(self):
        addresses = [f"198.51.{n // 250}.{n % 250}" for n in range(500)]     # within KEYS_KEPT
        for addr in addresses:
            self.stranger(addr=addr)
        self.assertEqual(500, len(self.kept), "the first refusal from each address is kept")
        for addr in addresses:
            self.stranger(addr=addr)
        self.assertEqual(500, len(self.kept), "past the ceiling, second refusals wait for the summary")
        self.stranger(addr="192.0.2.8", status=403, user="ada")
        self.assertEqual(403, self.kept[-1]["status"], "signed-in refusals have their own ceiling")
        self.clock[0] += AUDIT_LOG.WINDOW_SECONDS + AUDIT_LOG.SWEEP_SECONDS + 1
        AUDIT_LOG.record(view(), 200, 1)
        everyone = [line for line in self.summaries() if line.get("addr") == "*"]
        self.assertEqual([500], [line["suppressed"] for line in everyone])

    def test_the_table_is_bounded_and_an_evicted_flood_is_still_summed_up(self):
        for _ in range(9):
            self.stranger(addr="192.0.2.7")
        for n in range(AUDIT_LOG.KEYS_KEPT + 50):
            self.clock[0] += 0.001
            self.stranger(addr=f"198.51.{n // 250}.{n % 250}")
        self.assertLessEqual(len(AUDIT_LOG._buckets), AUDIT_LOG.KEYS_KEPT)
        self.assertEqual([9 - AUDIT_LOG.BURST],
                         [line["suppressed"] for line in self.summaries() if line.get("addr") == "192.0.2.7"])

    def test_summaries_are_file_only(self):
        for _ in range(9):
            self.stranger()
        self.clock[0] += AUDIT_LOG.WINDOW_SECONDS + 1
        self.stranger()
        self.assertFalse([line for line in self.stdout if "suppressed" in line])


class Notes(unittest.TestCase):
    """What a request notes is its own: never the next request's on the same thread."""

    def setUp(self):
        self.out = []
        self.enterContext(mock.patch.object(AUDIT_LOG, "emit", side_effect=lambda entry, keep=True: self.out.append(entry)))
        AUDIT_LOG._take_notes()

    def test_notes_never_reach_the_next_request(self):
        AUDIT_LOG.note(tool="container_restart", kind="change")
        AUDIT_LOG.record(view("GET", "/api/overview"), 200, 1)      # a read takes them too
        AUDIT_LOG.record(view(), 200, 1)
        self.assertNotIn("tool", self.out[0])
        AUDIT_LOG.note(to="member-2")
        with mock.patch.object(AUDIT_LOG, "line", side_effect=RuntimeError("bug")):
            AUDIT_LOG.record(view(), 200, 1)
        AUDIT_LOG.record(view(), 200, 1)
        self.assertNotIn("to", self.out[1])

    def test_a_relayed_request_names_the_cluster(self):
        import homestead_fleet as FLEET
        handler = mock.Mock(headers={}, command="POST", path="/api/workload/scale")
        with mock.patch.object(FLEET, "member", return_value={"id": "member-2", "name": "loft", "url": "https://192.0.2.20"}), \
                mock.patch.object(FLEET, "_connect", side_effect=OSError("no route")):
            with self.assertRaises(FLEET.Unreachable):
                FLEET.forward(handler, "member-2", b"{}", "ada", "operator")
        AUDIT_LOG.record(view(), 502, 1)
        self.assertEqual(("member-2", 502), (self.out[0]["to"], self.out[0]["status"]))


class Stdout(unittest.TestCase):
    def test_one_json_line_per_emit(self):
        with mock.patch.object(AUDIT_LOG.sys, "stdout") as out:
            AUDIT_LOG.emit({"v": 1, "path": "/api/é\n"})
        written = "".join(call.args[0] for call in out.write.call_args_list)
        self.assertEqual('{"v":1,"path":"/api/\\u00e9\\n"}\n', written)
        out.flush.assert_called()


if __name__ == "__main__":
    unittest.main()
