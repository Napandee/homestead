"""/metrics (#736): who may read it, and what it says."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import server
import homestead_routes as MODULE_ROUTES
import homestead_web_paths as WEB_PATHS


def guard(path, method="GET", headers=None):
    """server.H's guard for one request with no session, outside any fence."""
    h = object.__new__(server.H)
    h.command, h.path, h.headers = method, path, dict(headers or {})
    h._send = mock.Mock(); h._who = mock.Mock(return_value=None)
    h.client_address = ("192.0.2.10", 40000)
    with mock.patch.object(server.CFACCESS, "enabled", return_value=False), \
         mock.patch.object(server, "_self_data_boot_pending", False), \
         mock.patch.object(server, "require_self_data_write", return_value=None):
        result = h._guard(path)
    return h, result


class BarePathsAreNotPages(unittest.TestCase):
    def test_metrics_and_readyz_are_not_page_paths(self):
        self.assertFalse(WEB_PATHS.is_page_path("/metrics"))
        self.assertFalse(WEB_PATHS.is_page_path("/readyz"))

    def test_a_module_route_at_a_bare_path_needs_credentials(self):
        """A route a module declares at /metrics is not let through as a page."""
        fake = MODULE_ROUTES.Route("GET", "/metrics", "viewer", lambda request: "secret", "test")
        with mock.patch.dict(MODULE_ROUTES._table, {("GET", "/metrics"): fake}):
            h, result = guard("/metrics")
        self.assertTrue(result)
        self.assertEqual(401, h._send.call_args.args[0])

    def test_every_module_route_that_is_not_public_needs_credentials(self):
        """Any module route, at any path, that is not public answers 401 to
        a request with no session and no key: none is served as a page."""
        for (method, path) in MODULE_ROUTES.table():
            if WEB_PATHS.is_public_path(path):
                continue
            with self.subTest(method=method, path=path):
                h, result = guard(path, method)
                self.assertTrue(result, f"{method} {path} passed the guard without credentials")
                self.assertEqual(401, h._send.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
