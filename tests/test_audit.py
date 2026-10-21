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
        self.enterContext(mock.patch.object(AUDIT_LOG, "emit", side_effect=self.out.append))
        AUDIT_LOG._refusals.clear()
        self.addCleanup(AUDIT_LOG._refusals.clear)

    def test_a_read_writes_nothing_and_a_change_one_line(self):
        AUDIT_LOG.record(view("GET", "/api/overview"), 200, 3)
        AUDIT_LOG.record(view(), 200, 3)
        self.assertEqual(["/api/workload/scale"], [line["path"] for line in self.out])

    def test_refusal_throttle_is_bounded_and_spares_successes(self):
        clock = [1000.0]
        self.enterContext(mock.patch.object(AUDIT_LOG.time, "time", side_effect=lambda: clock[0]))
        stranger = view(user=None, role=None, addr="192.0.2.7")
        AUDIT_LOG.record(stranger, 401, 1)
        AUDIT_LOG.record(stranger, 403, 1)
        self.assertEqual(1, len(self.out))
        clock[0] += 61
        AUDIT_LOG.record(stranger, 401, 1)
        self.assertEqual(2, len(self.out))
        for _ in range(3):
            AUDIT_LOG.record(view("POST", "/api/auth/setup", user=None, role=None, addr="192.0.2.7"), 200, 1)
        self.assertEqual(5, len(self.out), "a no-user success (first admin) is never throttled")
        for n in range(AUDIT_LOG.REFUSAL_ADDRESSES + 1):
            AUDIT_LOG.record(view(user=None, role=None, addr=f"198.51.100.{n}"), 401, 1)
        self.assertLessEqual(len(AUDIT_LOG._refusals), AUDIT_LOG.REFUSAL_ADDRESSES)

    def test_record_never_raises(self):
        with mock.patch.object(AUDIT_LOG, "emit", side_effect=OSError("disk")):
            self.assertIsNone(AUDIT_LOG.record(view(), 200, 1))
        with mock.patch.object(AUDIT_LOG, "line", side_effect=RuntimeError("bug")):
            self.assertIsNone(AUDIT_LOG.record(view(), 200, 1))


class Stdout(unittest.TestCase):
    def test_one_json_line_per_emit(self):
        with mock.patch.object(AUDIT_LOG.sys, "stdout") as out:
            AUDIT_LOG.emit({"v": 1, "path": "/api/é\n"})
        written = "".join(call.args[0] for call in out.write.call_args_list)
        self.assertEqual('{"v":1,"path":"/api/\\u00e9\\n"}\n', written)
        out.flush.assert_called()


if __name__ == "__main__":
    unittest.main()
