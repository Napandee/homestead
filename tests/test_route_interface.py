"""What a module route may see of the connection and write back (#764).

A route gets the request's headers - without the ones that carry a secret -
and Homestead's own cookies, and who the guard found: a Cloudflare Access
sign-in, a linked Homestead, an API key. It can set Homestead's own cookies and
safe response headers through one checked writer; anything else is refused
with a 500 before any of it is written."""
import email.message
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import homestead_routes as ROUTES

C = ROUTES.Cookie


def bare_handler(sent=(), **attrs):
    """server.H as the tests build it: no socket, headers as the request sent them."""
    import server
    handler = object.__new__(server.H)
    headers = email.message.Message()
    for name, value in sent:
        headers[name] = value
    handler.headers = headers
    for name, value in attrs.items():
        setattr(handler, name, value)
    return handler


class Interface(unittest.TestCase):
    def test_old_requests_and_replies_still_build(self):
        r = ROUTES.Request("GET", "/api/x", {}, None, "ada", "viewer")
        self.assertEqual(("", None), (r.addr, r.conn))
        self.assertEqual(((), ()), (ROUTES.Reply(409, {"e": 1}).headers, ROUTES.Reply(409, {}).cookies))
        self.assertEqual("text/plain", ROUTES.Reply(200, "x", "text/plain").ctype)

    def test_cookie_header_matches_set_cookie_byte_for_byte(self):
        self.assertEqual("homestead_session=t; Path=/; HttpOnly; SameSite=Strict; Max-Age=60",
                         ROUTES.cookie_header(C("homestead_session", "t", max_age=60), tls=False))
        self.assertEqual("homestead_session=; Path=/; HttpOnly; SameSite=Strict; Secure; Max-Age=0",
                         ROUTES.cookie_header(C("homestead_session", "", max_age=0), tls=True))
        self.assertEqual("homestead_cluster=node1; Path=/; HttpOnly; SameSite=Strict",
                         ROUTES.cookie_header(C("homestead_cluster", "node1"), tls=False))

    def test_cookie_header_is_what_server_py_writes(self):
        import server
        handler = bare_handler([("X-Forwarded-Proto", "https")], _extra_headers=[])
        handler._set_cookie("tok", max_age=60)
        handler._set_cookie("", clear=True)
        self.assertEqual([("Set-Cookie", ROUTES.cookie_header(C(server.AUTH.COOKIE, "tok", max_age=60), tls=True)),
                          ("Set-Cookie", ROUTES.cookie_header(C(server.AUTH.COOKIE, "", max_age=0), tls=True))],
                         handler._extra_headers)

    def test_only_homesteads_cookies_with_plain_values(self):
        for bad in (C("CF_Authorization", "x"), C("homestead_session", "a;b"), C("homestead_session", "a b"),
                    C("homestead_session", "a,b"), C("homestead_session", "a\r\n"), C("homestead_session", "a\x00"),
                    C("homestead_session", "a", path="x"), C("homestead_session", "a", path="/a;b"),
                    C("homestead_session", "a", max_age="1; Domain=example.com"), C("homestead_session", "a", max_age=-1)):
            with self.subTest(cookie=bad), self.assertRaises(ROUTES.Refused):
                ROUTES.cookie_header(bad, tls=False)

    def test_headers_refuse_injection_and_owned_names(self):
        for name, value in (("X-A", "a\r\nSet-Cookie: x=1"), ("X-A", "a\nb"), ("Bad Name", "v"), ("X:A", "v"),
                            ("Set-Cookie", "x=1"), ("content-security-policy", "x"), ("Cache-Control", "max-age=9"),
                            ("X-A", "☃")):
            with self.subTest(name=name, value=value), self.assertRaises(ROUTES.Refused):
                ROUTES.check_header(name, value)
        self.assertEqual(("Content-Disposition", 'attachment; filename="a.zip"'),
                         ROUTES.check_header("Content-Disposition", 'attachment; filename="a.zip"'))

    def test_location_refuses_scheme_relative_and_backslash(self):      # Review Focus 1
        self.assertEqual(("Location", "/"), ROUTES.check_header("Location", "/"))
        for bad in ("//example.com", "/\\example.com", "https://example.com", "example.com", "/a b"):
            with self.subTest(location=bad), self.assertRaises(ROUTES.Refused):
                ROUTES.check_header("Location", bad)

    def test_refused_is_not_a_value_error(self):
        # do_* answers a ValueError with 400; a refused reply is Homestead's own fault: 500.
        self.assertFalse(issubclass(ROUTES.Refused, ValueError))


class PrefixRoutes(unittest.TestCase):
    def setUp(self):
        ROUTES.table()
        self.added = []
        for path, role in (("/api/t/*", "admin"), ("/api/t/exact", "viewer"), ("/api/t/u/*", "operator")):
            ROUTES._table[("GET", path)] = ROUTES.Route("GET", path, role, lambda request: {}, "test")
            self.added.append(("GET", path))

    def tearDown(self):
        for key in self.added:
            ROUTES._table.pop(key, None)

    def test_prefix_routes(self):
        self.assertEqual("/api/t/*", ROUTES.find("GET", "/api/t/a/b").path)
        self.assertEqual("/api/t/exact", ROUTES.find("GET", "/api/t/exact").path)
        self.assertIsNone(ROUTES.find("GET", "/api/t"))
        self.assertIsNone(ROUTES.find("GET", "/api/tx"))
        self.assertIsNone(ROUTES.find("POST", "/api/t/a"))
        self.assertEqual("/api/t/u/*", ROUTES.find("GET", "/api/t/u/v").path)
        self.assertEqual("admin", ROUTES.role("/api/t/a", "GET"))

    def test_a_star_anywhere_but_the_end_is_refused(self):
        for path in ("/api/a*/b", "/api/a*", "/api/*/b"):
            module = type(sys)("homestead_test_star")
            module.ROUTES = {("GET", path): ("viewer", lambda request: {})}
            with self.subTest(path=path), mock.patch.dict(sys.modules, {"homestead_test_star": module}), \
                    mock.patch.object(ROUTES, "MODULES", ("homestead_test_star",)), \
                    mock.patch.object(ROUTES, "_table", {}), self.assertRaises(ValueError):
                ROUTES.table()


class Connections(unittest.TestCase):
    def test_conn_headers_drop_secrets_in_any_case(self):               # D2 + Review Focus 4
        sent = [("Cookie", "homestead_session=s1"), ("cookie", "x=2"), ("authorization", "Bearer hs_k"),
                ("PROXY-AUTHORIZATION", "Basic eA=="), ("Cf-Access-Jwt-Assertion", "a.b.c"),
                ("X-Homestead-Fleet", "loft1"), ("X-Homestead-Fleet-Time", "1"), ("x-homestead-fleet-nonce", "n"),
                ("X-Homestead-Fleet-User", "ada"), ("X-Homestead-Fleet-Role", "admin"),
                ("X-Homestead-Fleet-Signature", "sig"), ("User-Agent", "UA"), ("Origin", "http://192.0.2.1")]
        handler = bare_handler(sent, _fleet_who=None)
        conn = ROUTES.connection(handler)
        gone = ("cookie", "authorization", "proxy-authorization", "cf-access-jwt-assertion", "x-homestead-fleet",
                "x-homestead-fleet-time", "x-homestead-fleet-nonce", "x-homestead-fleet-user",
                "x-homestead-fleet-role", "x-homestead-fleet-signature")
        for name in gone:
            for spelled in (name, name.upper(), name.title()):
                self.assertIsNone(conn.headers.get(spelled), spelled)
                self.assertNotIn(spelled, conn.headers)
                with self.assertRaises(KeyError):
                    conn.headers[spelled]
        self.assertEqual(set(), {n.lower() for n in conn.headers} & set(gone))
        self.assertFalse(any(secret in repr(conn.headers) for secret in ("s1", "hs_k", "eA==", "a.b.c", "sig")))
        self.assertEqual("UA", conn.headers.get("user-agent"))
        self.assertEqual("UA", conn.headers["USER-AGENT"])
        self.assertEqual("x", conn.headers.get("X-Missing", "x"))
        self.assertEqual(2, len(conn.headers))
        with self.assertRaises(TypeError):
            conn.headers["X-A"] = "1"

    def test_conn_headers_from_a_plain_dict(self):
        handler = bare_handler()
        handler.headers = {"Authorization": "Bearer x", "Accept": "text/html"}
        self.assertEqual(["Accept"], list(ROUTES.connection(handler).headers))

    def test_conn_cookies_are_homesteads_only(self):
        conn = ROUTES.connection(bare_handler([("Cookie", "homestead_session=s; CF_Authorization=j; other=1")]))
        self.assertEqual({"homestead_session": "s"}, dict(conn.cookies))
        with self.assertRaises(TypeError):
            conn.cookies["homestead_cluster"] = "x"

    def test_identities_come_from_the_guard(self):
        key = {"name": "ha", "kind": "key", "scopes": ["read"]}
        handler = bare_handler([("X-Forwarded-Proto", "https"), ("Cf-Ray", "1")],
                               _access={"email": "ada@example.com"}, _fleet_from="node1", api_key=key)
        conn = ROUTES.connection(handler)
        self.assertEqual(({"email": "ada@example.com"}, "node1", key), (conn.access, conn.fleet, conn.key))
        self.assertEqual((True, True), (conn.tls, conn.cloudflare))
        self.assertEqual(handler._who, conn.who)

    def test_a_signed_public_request_names_its_sender(self):
        handler = bare_handler([("X-Homestead-Fleet", "node1")], _fleet_who=False, _fleet_from=None)

        def identity():
            handler._fleet_who, handler._fleet_from = None, "node1"
        handler._fleet_identity = identity
        self.assertEqual("node1", ROUTES.connection(handler).fleet)

    def test_connection_on_a_bare_handler(self):     # Review Focus 5
        import server
        handler = object.__new__(server.H)
        handler.headers = {}
        conn = ROUTES.connection(handler)
        self.assertEqual((False, False, None, None, None, {}),
                         (conn.tls, conn.cloudflare, conn.access, conn.fleet, conn.key, dict(conn.cookies)))
        handler.headers = {"X-Forwarded-Proto": "https"}
        self.assertTrue(ROUTES.connection(handler).tls)

    def test_a_route_that_needs_conn_refuses_an_in_process_call(self):
        with self.assertRaises(PermissionError):
            ROUTES.connected(ROUTES.Request("GET", "/api/x", {}, None, "ada", "admin"))
        conn = ROUTES.connection(bare_handler())
        self.assertIs(conn, ROUTES.connected(ROUTES.Request("GET", "/api/x", {}, None, "ada", "admin", "", conn)))


class Answering(unittest.TestCase):
    """A module's Reply is written through server.H.do_POST."""

    def handler(self, path, answer, guard_headers=()):
        import server
        handler = object.__new__(server.H)
        handler.path, handler.headers, handler.command = path, {}, "POST"
        handler._client_ip = lambda: "127.0.0.1"
        handler._send = mock.Mock()
        handler._send_stream = mock.Mock()
        handler.user, handler.role = "ada", "admin"
        handler._body = lambda: {}

        def guard(path):
            handler._extra_headers.extend(guard_headers)
            return False
        handler._guard = guard
        ROUTES.table()
        ROUTES._table[("POST", path)] = ROUTES.Route("POST", path, "admin", lambda request: answer, "test")
        self.addCleanup(ROUTES._table.pop, ("POST", path), None)
        return handler

    def test_reply_cookies_and_headers_are_written_after_the_guards(self):
        guards = ("Set-Cookie", "homestead_session=old; Path=/; HttpOnly; SameSite=Strict; Max-Age=60")
        handler = self.handler("/api/test/reply", ROUTES.Reply(
            200, {"ok": True}, cookies=(C("homestead_cluster", "", max_age=0),), headers=(("Location", "/"),)),
            [guards])
        handler.do_POST()
        self.assertEqual([guards, ("Set-Cookie", "homestead_cluster=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"),
                          ("Location", "/")], handler._extra_headers)
        self.assertEqual((200, {"ok": True}, "application/json"), handler._send.call_args.args)

    def test_a_refused_reply_is_a_500_and_carries_none_of_its_cookies(self):   # D4
        handler = self.handler("/api/test/refused", ROUTES.Reply(
            200, {}, cookies=(C("homestead_session", "ok", max_age=1),), headers=(("Set-Cookie", "x=1"),)))
        handler.do_POST()
        self.assertEqual(500, handler._send.call_args.args[0])
        self.assertFalse(any("homestead_session=ok" in value for _, value in handler._extra_headers))
        self.assertFalse(any(value == "x=1" for _, value in handler._extra_headers))

    def test_raw_stream_and_plain_answers_are_unchanged(self):
        handler = self.handler("/api/test/raw", ROUTES.Raw(b"PNG", "image/png"))
        handler.do_POST()
        self.assertEqual((200, b"PNG", "image/png"), handler._send.call_args.args)
        self.assertEqual([("X-Content-Type-Options", "nosniff"), ("Cache-Control", "no-store")], handler._extra_headers)
        stream = ROUTES.Stream(iter([b"a"]), "application/zip", "a.zip", 1)
        handler = self.handler("/api/test/stream", stream)
        handler.do_POST()
        handler._send_stream.assert_called_once_with(stream)
        handler._send.assert_not_called()
        handler = self.handler("/api/test/plain", {"ok": True})
        handler.do_POST()
        self.assertEqual((200, {"ok": True}), handler._send.call_args.args)
        self.assertEqual([], handler._extra_headers)

    def test_a_route_reads_its_connection(self):
        seen = []
        handler = self.handler("/api/test/conn", {"ok": True})
        ROUTES._table[("POST", "/api/test/conn")] = ROUTES.Route(
            "POST", "/api/test/conn", "admin", lambda request: seen.append(request) or {"ok": True}, "test")
        handler.headers = {"User-Agent": "UA", "Cookie": "homestead_session=s"}
        handler.do_POST()
        conn = ROUTES.connected(seen[0])
        self.assertEqual(("UA", None, {"homestead_session": "s"}),
                         (conn.headers.get("User-Agent"), conn.headers.get("Cookie"), dict(conn.cookies)))


if __name__ == "__main__":
    unittest.main()
