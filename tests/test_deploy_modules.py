"""homestead_deploy split by feature (#623): the App Store catalogue lives in
homestead_deploy_appstore and the Deployment builders in
homestead_deploy_manifests, and homestead_deploy keeps the bound helpers, the
routes and a re-export of every name, so other modules, server.py and tests
keep working."""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

import homestead_deploy as DEPLOY
import homestead_deploy_appstore as APPSTORE
import homestead_deploy_manifests as MANIFESTS

PARTS = (APPSTORE, MANIFESTS)


class DeployModuleTests(unittest.TestCase):
    def test_every_moved_name_is_the_same_object_on_homestead_deploy(self):
        for part in PARTS:
            moved = [name for name in vars(part) if not name.startswith("__")
                     and getattr(vars(part)[name], "__module__", part.__name__) == part.__name__
                     and name not in ("D",) and not isinstance(vars(part)[name], type(sys))]
            self.assertTrue(moved, part.__name__)
            for name in moved:
                self.assertIs(getattr(DEPLOY, name), getattr(part, name), f"{part.__name__}.{name}")

    def test_each_module_can_be_imported_first(self):
        for name in ("homestead_deploy_appstore", "homestead_deploy_manifests", "homestead_deploy"):
            done = subprocess.run([sys.executable, "-c",
                                   f"import {name}, homestead_deploy as D; D.fetch_appstore; D.build_deployment"],
                                  cwd=SERVER, capture_output=True, text=True)
            self.assertEqual(0, done.returncode, f"importing {name} first: {done.stderr}")

    def test_the_routes_are_unchanged(self):
        self.assertEqual({
            ("GET", "/api/deploy/options"), ("GET", "/api/appstore"), ("GET", "/api/appstore/app"),
            ("POST", "/api/appstore/install"), ("POST", "/api/preview"), ("POST", "/api/deploy"),
            ("POST", "/api/compose/parse"), ("POST", "/api/compose/preview"),
            ("POST", "/api/compose/apply")}, set(DEPLOY.ROUTES))
        for part in PARTS:
            self.assertFalse(hasattr(part, "ROUTES"), part.__name__)

    def test_helpers_bound_on_homestead_deploy_reach_the_moved_code(self):
        # server.py binds cached and get_app_settings on homestead_deploy (bind), and tests replace them there.
        with mock.patch.object(DEPLOY, "cached", lambda key, ttl, read: read(), create=True), \
                mock.patch.object(DEPLOY, "get_app_settings",
                                  lambda: {"catalog_url": "https://feed.example.com/apps.json"}, create=True):
            self.assertEqual("https://feed.example.com/apps.json", DEPLOY.catalog_source())

    def test_a_replacement_on_homestead_deploy_reaches_the_moved_code(self):
        # tests/test_deploy.py replaces analyze_deploy_intent on homestead_deploy.
        with mock.patch.object(DEPLOY, "analyze_deploy_intent", return_value={"seen": True}):
            self.assertEqual({"seen": True}, DEPLOY.template_to_cfg({"name": "app", "repo": "nginx", "config": []}))


if __name__ == "__main__":
    unittest.main()
