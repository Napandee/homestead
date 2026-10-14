"""Routes declared beside their handlers (homestead_routes.py, #363), and the
guard that keeps server.py to routing: it may only shrink."""
import re
import sys
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import homestead_route_policy as POLICY
import homestead_routes as ROUTES

SERVER = (ROOT / "server" / "server.py").read_text(encoding="utf-8")

# Lower these as routes move out of server.py; never raise them.
MAX_LINES = 4433
MAX_PATH_BRANCHES = 57
# How far below MAX_LINES server.py may fall before MAX_LINES has to come down.
RATCHET_SLACK = 20
# Lines in server.py's alias block (names tests still patch on server). Only lowered.
ALIASES = 0
ALIAS_START = "# Names tests still patch on server; track 2 moves their tests."
ALIAS_END = "# End of the names tests still patch on server."

# The routes server.py's handler still answers itself, each with the reason it
# needs the handler. Every other route is declared in its module's ROUTES.
INLINE_ROUTES = (
    ("GET", "/api/v1/", "connection-bound: API keys and sessions (homestead_api_v1)"),
    ("GET", "/api/diagnostics/download", "connection-bound: sets Content-Disposition"),
    ("GET", "/api/console", "connection-bound: WebSocket upgrade"),
    ("GET", "/api/node/shell", "connection-bound: WebSocket upgrade"),
    ("GET", "/api/vm/console", "connection-bound: WebSocket upgrade"),
    ("GET", "/api/icons/", "connection-bound: serves a file with its own headers (path prefix)"),
    ("GET", "/api/auth/state", "connection-bound: cookies/session"),
    ("GET", "/api/fleet", "connection-bound: fleet forwarding"),
    ("GET", "/api/fleet/state", "connection-bound: fleet forwarding"),
    ("GET", "/api/fleet/home", "connection-bound: cookies/session"),
    ("GET", "/api/fleet/all/", "connection-bound: fleet forwarding"),
    ("GET", "/api/auth/preferences/dashboard", "auth: stays with sign-in and sessions"),
    ("GET", "/api/self/data/prepare", "to move in Part 7"),
    ("GET", "/api/self/data/handoff/", "stays: the handler matches the handoff id in the path by prefix (path prefix)"),
    ("GET", "/api/snapshot-files/plan", "to move in Part 4"),
    ("GET", "/api/snapshot-files/status", "to move in Part 4"),
    ("GET", "/api/snapshot-files/list", "to move in Part 4"),
    ("GET", "/api/snapshot-files/download", "to move in Part 4"),
    ("GET", "/api/logos", "to move in Part 5"),
    ("GET", "/api/logos/missing", "to move in Part 5"),
    ("POST", "/api/v1/", "connection-bound: API keys and sessions (homestead_api_v1)"),
    ("POST", "/api/auth/keys", "connection-bound: fleet forwarding (refused when relayed)"),
    ("POST", "/api/auth/keys/revoke", "connection-bound: fleet forwarding (refused when relayed)"),
    ("POST", "/api/auth/keys/edit", "connection-bound: fleet forwarding (refused when relayed)"),
    ("POST", "/api/fleet/", "connection-bound: fleet forwarding"),
    ("POST", "/api/config/backup", "to move in Part 8"),
    ("POST", "/api/config/inspect", "to move in Part 8"),
    ("POST", "/api/config/restore", "to move in Part 8"),
    ("POST", "/api/auth/setup", "connection-bound: cookies/session"),
    ("POST", "/api/auth/login", "connection-bound: cookies/session"),
    ("POST", "/api/auth/logout", "connection-bound: cookies/session"),
    ("POST", "/api/auth/preferences/dashboard", "auth: stays with sign-in and sessions"),
    ("POST", "/api/auth/password", "connection-bound: cookies/session"),
    ("POST", "/api/auth/users", "connection-bound: sign-in history (address, User-Agent)"),
    ("POST", "/api/auth/role", "connection-bound: sign-in history (address, User-Agent)"),
    ("POST", "/api/auth/users/delete", "connection-bound: sign-in history (address, User-Agent)"),
    ("POST", "/api/auth/signout-everywhere", "connection-bound: cookies/session"),
    ("POST", "/api/self/data/abandon", "to move in Part 7"),
    ("POST", "/api/self/data/move", "connection-bound: cookies/session"),
    ("POST", "/api/self/data/move/preview", "to move in Part 7"),
    ("POST", "/api/self/data/handoff/close", "to move in Part 7"),
    ("POST", "/api/self/data/handoff/close/preview", "to move in Part 7"),
    ("POST", "/api/self/data/prepare/archive", "to move in Part 7"),
    ("POST", "/api/self/data/prepare/archive/preview", "to move in Part 7"),
    ("POST", "/api/self/data/prepare", "to move in Part 7"),
    ("POST", "/api/self/data/prepare/preview", "to move in Part 7"),
    ("POST", "/api/helm/install", "to move in Part 6"),
    ("POST", "/api/helm/upgrade", "to move in Part 6"),
    ("POST", "/api/helm/uninstall", "to move in Part 6"),
    ("POST", "/api/snapshot-files/start", "to move in Part 4"),
    ("POST", "/api/snapshot-files/close", "to move in Part 4"),
    ("POST", "/api/self/samba", "to move in Part 4"),
    ("POST", "/api/self/nfs", "to move in Part 4"),
    ("POST", "/api/addons/smb/remove", "to move in Part 4"),
    ("POST", "/api/addons/nfs/remove", "to move in Part 4"),
    ("ANY", "/api/fleet/accept", "stays: fleet forwarding (_fleet_post)"),
    ("ANY", "/api/fleet/address", "stays: fleet forwarding (_fleet_post)"),
    ("ANY", "/api/fleet/join", "stays: fleet forwarding (_fleet_post)"),
    ("ANY", "/api/fleet/leave", "stays: fleet forwarding (_fleet_post)"),
    ("ANY", "/api/fleet/link-legacy", "stays: fleet forwarding (_fleet_post)"),
    ("ANY", "/api/fleet/remove", "stays: fleet forwarding (_fleet_post)"),
    ("ANY", "/api/fleet/switch", "stays: fleet forwarding (_fleet_post)"),
    ("ANY", "/api/fleet/sync", "stays: fleet forwarding (_fleet_post)"),
    ("ANY", "/api/v1/openapi.json", "connection-bound: API keys and sessions (homestead_api_v1)"),
    ("ANY", "/api/auth/", "not a route: a prefix _send tests before it hides a diagnostic report"),
    ("ANY", "/api/diagnostics", "not a route: a prefix _send tests before it hides a diagnostic report"),
)


def handler_body(method):
    """The text of server.py's do_GET, do_POST, ... method."""
    lines = SERVER.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith(f"def do_{method}(self"))
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"    def |\S", lines[i])), len(lines))
    return "\n".join(lines[start:end])


def literals_in(text):
    """Every "/api/..." path written in text, however it is quoted or matched
    (single or double quotes, a regex, an f-string, a "?" tail). A path compared
    with a constant (p == MCP.PATH) has no literal and cannot be seen here."""
    return set(re.findall(r'["\'](/api/[A-Za-z0-9/._-]*)', text))


def api_literals(method):
    """Every "/api/..." path written in server.py's do_GET, do_POST, ..."""
    return literals_in(handler_body(method))


def helper_literals():
    """Every "/api/..." path written in a method of class H other than do_*:
    helpers such as _fleet_post answer routes too."""
    lines = SERVER.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("class H("))
    end = next(i for i in range(start + 1, len(lines)) if re.match(r"\S", lines[i]))
    keep, inside = [], False
    for line in lines[start:end]:
        match = re.match(r"    def (\w+)\(", line)
        if match:
            inside = not match.group(1).startswith("do_")
        if inside:
            keep.append(line)
    return literals_in("\n".join(keep))


class DeclaredRoutes(unittest.TestCase):
    def test_every_route_has_a_role_and_a_handler(self):
        table = ROUTES.table()
        self.assertTrue(table)
        for (method, path), route in table.items():
            self.assertIn(method, ("GET", "POST", "PUT", "DELETE"), path)
            self.assertTrue(path.startswith("/api/"), path)
            self.assertIn(route.role, ROUTES.ROLES, path)
            self.assertTrue(callable(route.handler), path)

    def test_a_route_and_its_role_are_written_once(self):
        for method, path in ROUTES.table():
            self.assertNotIn((method, path), POLICY.POLICY, f"{method} {path} is declared in its module")

    def test_the_policy_reads_the_module_role(self):
        self.assertEqual("viewer", POLICY.role("/api/firewall", "GET"))
        self.assertEqual("admin", POLICY.role("/api/firewall/save", "POST"))
        self.assertIsNone(POLICY.role("/api/firewall/save", "GET"))

    def test_server_py_has_no_branch_for_a_declared_route(self):
        for method, path in ROUTES.table():
            self.assertNotIn(f'p == "{path}"', handler_body(method), f"{method} {path} is answered by its module")

    def test_handlers_get_the_request(self):
        seen = []
        ROUTES._table[("POST", "/api/test/echo")] = ROUTES.Route("POST", "/api/test/echo", "admin",
                                                                  lambda request: seen.append(request) or {"ok": True}, "test")
        try:
            route = ROUTES.find("post", "/api/test/echo")
            answer = route.handler(ROUTES.Request("POST", "/api/test/echo", {}, {"a": 1}, "ada", "admin"))
        finally:
            del ROUTES._table[("POST", "/api/test/echo")]
        self.assertEqual({"ok": True}, answer)
        self.assertEqual({"a": 1}, seen[0].body)
        self.assertEqual("ada", seen[0].user)
        self.assertIsNone(ROUTES.find("GET", "/api/test/echo"))


class ThroughTheServer(unittest.TestCase):
    """A request for a declared route reaches its module through server.py's
    handler, after the guard, and its errors are answered as before."""

    def handler(self, method, path, body=None):
        import server
        handler = object.__new__(server.H)
        handler.path, handler.headers, handler.command = path, {}, method
        handler._guard = lambda path: False
        handler._body = lambda: body or {}
        handler._client_ip = lambda: "127.0.0.1"
        handler._send = mock.Mock()
        handler.user, handler.role = "ada", "admin"
        return server, handler

    def test_a_post_reaches_the_module(self):
        server, handler = self.handler("POST", "/api/firewall/save", {"name": "web"})
        with mock.patch.object(server.FIREWALL, "save", return_value={"ok": True, "saved": "web"}) as save:
            handler.do_POST()
        save.assert_called_once_with({"name": "web"})
        self.assertEqual((200, {"ok": True, "saved": "web"}), handler._send.call_args.args)

    def test_a_get_reaches_the_module(self):
        server, handler = self.handler("GET", "/api/firewall")
        with mock.patch.object(server.FIREWALL, "inventory", return_value={"policies": []}):
            handler.do_GET()
        self.assertEqual((200, {"policies": []}), handler._send.call_args.args)

    def test_a_reply_keeps_its_own_status(self):
        server, handler = self.handler("POST", "/api/test/busy")
        ROUTES._table[("POST", "/api/test/busy")] = ROUTES.Route(
            "POST", "/api/test/busy", "admin", lambda request: ROUTES.Reply(409, {"error": "busy", "plan": []}), "test")
        try:
            handler.do_POST()
        finally:
            del ROUTES._table[("POST", "/api/test/busy")]
        self.assertEqual((409, {"error": "busy", "plan": []}, "application/json"), handler._send.call_args.args)

    def test_a_reply_keeps_its_status(self):
        # A drain that would strand workloads is refused with what it would strand.
        server, handler = self.handler("POST", "/api/node/drain", {"node": "node1"})
        impact = {"stranded": [{"ns": "lab", "name": "web"}]}
        with mock.patch.object(server.PLACE, "impact", return_value=impact), \
                mock.patch.object(server.LC, "drain") as drain:
            handler.do_POST()
        code, body = handler._send.call_args.args[:2]
        self.assertEqual((409, impact), (code, body["impact"]))
        drain.assert_not_called()

    def test_a_module_error_is_answered_as_server_py_answers_its_own(self):
        server, handler = self.handler("POST", "/api/firewall/delete", {"name": "gone"})
        with mock.patch.object(server.FIREWALL, "remove", side_effect=ValueError("The policy no longer exists")):
            handler.do_POST()
        code, body = handler._send.call_args.args
        self.assertEqual(400, code)
        self.assertIn("no longer exists", body["error"])


class SharedCache(unittest.TestCase):
    def test_a_module_route_answers_from_server_py_s_cache_under_the_same_key(self):
        # server.py drops "network" after a VIP repair; the route must see that.
        import server
        server._cache.pop("network", None)
        with mock.patch.object(server.NETWORK, "inventory", side_effect=[{"n": 1}, {"n": 2}]) as inventory:
            route = ROUTES.find("GET", "/api/network")
            first = route.handler(ROUTES.Request("GET", "/api/network", {}, None, "ada", "viewer"))
            again = route.handler(ROUTES.Request("GET", "/api/network", {}, None, "ada", "viewer"))
            self.assertEqual(({"n": 1}, {"n": 1}), (first, again))
            server._cache.pop("network", None)
            self.assertEqual({"n": 2}, route.handler(ROUTES.Request("GET", "/api/network", {}, None, "ada", "viewer")))
        self.assertEqual(2, inventory.call_count)


class RouteLiterals(unittest.TestCase):
    def test_every_written_form_of_a_path_is_seen(self):
        for form in ('p == "/api/x/new"', "p == '/api/x/new'", 're.fullmatch(r"/api/x/new/[a-f0-9]{24}", p)',
                     'f"/api/x/new/{name}"', 'p.startswith("/api/x/new?")'):
            with self.subTest(form=form):
                self.assertTrue(any(path.startswith("/api/x/new") for path in literals_in(f"if {form}:")), form)


class ServerStaysRouting(unittest.TestCase):
    """CONTRIBUTING.md: server.py is the composition root - imports, constants,
    each module's bind(), start-up and a thin handler - and each feature lives
    in its own homestead_*.py module. These guards keep it that way."""

    def test_server_py_is_at_its_cap(self):
        """The ratchet: server.py is at most MAX_LINES lines, and at least
        MAX_LINES - RATCHET_SLACK. Growing fails: put the code in a module.
        Shrinking further fails until MAX_LINES is lowered to the new length,
        in the same commit, so the lines a move frees can't be spent again."""
        lines = len(SERVER.splitlines())
        self.assertLessEqual(lines, MAX_LINES,
                             "server.py grew: put the new code in a homestead_*.py module")
        self.assertGreaterEqual(lines, MAX_LINES - RATCHET_SLACK,
                                f"server.py shrank to {lines} lines: lower MAX_LINES to {lines}")

    def test_no_new_path_branches(self):
        branches = len(re.findall(r'\bp == "/api/', SERVER))
        self.assertLessEqual(branches, MAX_PATH_BRANCHES,
                             "a new route belongs in its module's ROUTES (homestead_routes.py), not an if in server.py")

    def test_every_inline_route_is_listed_with_its_reason(self):
        """The allow-list: every "/api/..." path written in class H is in
        INLINE_ROUTES with the reason it needs the handler (cookies, request
        headers, fleet forwarding, a WebSocket): under its method when it is in
        do_GET, do_POST or do_DELETE, under "ANY" when only a helper method
        (such as _fleet_post) writes it. A new route goes in its module's ROUTES
        instead. A route that moves out of server.py comes off INLINE_ROUTES in
        the same commit. Only literals are seen: a path compared with a constant
        (p == MCP.PATH) is not."""
        listed = {(method, path) for method, path, _ in INLINE_ROUTES}
        found = {(method, path) for method in ("GET", "POST", "DELETE") for path in api_literals(method)}
        found |= {("ANY", path) for path in helper_literals() if path not in {path for _, path in found}}
        self.assertEqual([], [f"{method} {path}" for method, path in sorted(found - listed)],
                         "answered inline in server.py: declare it in its module's ROUTES (homestead_routes.py), "
                         "or add it to INLINE_ROUTES with the reason it needs the handler")
        self.assertEqual([], [f"{method} {path}" for method, path in sorted(listed - found)],
                         "no longer answered inline in server.py: take it off INLINE_ROUTES")
        self.assertEqual(len(INLINE_ROUTES), len(listed), "a route is listed twice in INLINE_ROUTES")
        self.assertEqual([], [f"{method} {path}" for method, path, reason in INLINE_ROUTES if not reason.strip()],
                         "say why the handler answers it")

    def test_the_alias_block_only_shrinks(self):
        """The alias cap: names tests still patch on server
        (mock.patch.object(server, "X")) after their code moved are kept only
        between ALIAS_START and ALIAS_END, one `X = MODULE.X` a line, exactly
        ALIASES of them: more fails (point the test at the module instead),
        fewer fails until ALIASES is lowered, like the ratchet. Track 2 points
        those tests at the module and lowers ALIASES."""
        lines = SERVER.splitlines()
        self.assertEqual((1, 1), (lines.count(ALIAS_START), lines.count(ALIAS_END)),
                         "server.py keeps one alias block, opened and closed by ALIAS_START and ALIAS_END")
        start, end = lines.index(ALIAS_START), lines.index(ALIAS_END)
        self.assertLess(start, end, "ALIAS_START has to come before ALIAS_END in server.py")
        block = [line for line in lines[start + 1:end] if line.strip()]
        self.assertEqual([], [line for line in block if not re.fullmatch(r"[A-Za-z_]\w* = [A-Z][A-Z0-9_]*\.[A-Za-z_]\w*", line)],
                         "the alias block holds only `X = MODULE.X` lines")
        self.assertLessEqual(len(block), ALIASES,
                             f"server.py's alias block has {len(block)} lines (cap {ALIASES}): "
                             "point the test at the module the code lives in, not at server")
        self.assertGreaterEqual(len(block), ALIASES, f"the alias block shrank to {len(block)}: lower ALIASES to {len(block)}")

    def test_tests_patch_only_what_server_py_defines(self):
        """A test that patches a name on server (mock.patch.object(server, "X")
        or server.X = ...) must patch something server.py defines. A name
        server.py only imports back from a module (`from homestead_x import X`)
        is a copy: patching it changes nothing the module's code calls, and
        the test passes for the wrong reason. Patch the module instead."""
        imported = {}
        for module, names in re.findall(r"^from (homestead_\w+) import \(([^)]*)\)", SERVER, re.M):
            for name in re.findall(r"\w+", names):
                imported[name] = module
        offenders = []
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            text = path.read_text(encoding="utf-8")
            patched = set(re.findall(r'patch\.object\(server, "(\w+)"', text)) | set(re.findall(r"^\s*server\.(\w+) = ", text, re.M))
            offenders += [f"{path.name} patches server.{name}, which server.py imports from {imported[name]}: "
                          f"patch {imported[name]}.{name}" for name in sorted(patched & set(imported))]
        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
