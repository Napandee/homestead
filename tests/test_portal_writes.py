"""The portal page saves its whole list of links, so a save names the version
of the links the page was showing: links changed in another tab, or on
another copy of Homestead, since the editor opened are not saved over (#621)."""
import json
import os
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
import homestead_portal as PORTAL
import homestead_routes as ROUTER
from fakes.kube import Kube

MAP = "/api/v1/namespaces/lab/configmaps/homestead-portal"


def links(*titles):
    return [{"id": f"{i + 1:08x}", "title": title, "url": f"http://{title.lower()}.example.com", "section": "", "icon": ""}
            for i, title in enumerate(titles)]


def portal_map(rows):
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": "homestead-portal", "namespace": "lab", "labels": {"homestead.io/managed": "true"}},
            "data": {"links.json": json.dumps(rows)}}


def route(method, body=None):
    handler = PORTAL.ROUTES[(method, "/api/portal")][1]
    return handler(types.SimpleNamespace(body=body or {}, query={}))


class PortalWriteTests(unittest.TestCase):
    def setUp(self):
        saved = (PORTAL.kget, PORTAL.ksend, PORTAL.NAMESPACE, PORTAL.workloads, PORTAL.persist_icon, PORTAL.icon_data)
        self.addCleanup(lambda: PORTAL.bind(*saved))
        PORTAL.bind(K.kget, K.send, "lab", lambda: [], lambda source: source, lambda ref: "")

    def titles(self, kube):
        return [row["title"] for row in json.loads(kube.object(MAP)["data"]["links.json"])]

    def test_the_page_is_told_the_version_of_the_links_it_shows(self):
        with Kube(portal_map(links("Gateway"))) as kube:
            shown = route("GET")
            self.assertEqual(kube.object(MAP)["metadata"]["resourceVersion"], shown["version"])
        with Kube():
            self.assertEqual("", route("GET")["version"], "no links stored yet")

    def test_links_changed_since_the_editor_opened_are_not_saved_over(self):
        with Kube(portal_map(links("Gateway"))) as kube:
            opened = route("GET")
            # Another tab adds a link meanwhile.
            route("POST", {"links": links("Gateway", "NAS"), "version": route("GET")["version"]})
            answer = route("POST", {"links": links("Gateway", "Printer"), "version": opened["version"]})
            self.assertIsInstance(answer, ROUTER.Reply)
            self.assertEqual(409, answer.code)
            self.assertIn("changed", answer.body["error"])
            self.assertEqual(["Gateway", "NAS"], self.titles(kube), "the other tab's link was lost")

    def test_a_save_on_the_version_shown_is_kept_and_says_the_new_version(self):
        with Kube(portal_map(links("Gateway"))) as kube:
            result = route("POST", {"links": links("Gateway", "NAS"), "version": route("GET")["version"]})
            self.assertEqual(["Gateway", "NAS"], self.titles(kube))
            self.assertEqual(kube.object(MAP)["metadata"]["resourceVersion"], result["version"])
            # Saved again from the same page, on the version the save gave it.
            route("POST", {"links": links("Gateway"), "version": result["version"]})
            self.assertEqual(["Gateway"], self.titles(kube))

    def test_links_first_saved_elsewhere_are_not_saved_over(self):
        with Kube() as kube:
            opened = route("GET")
            route("POST", {"links": links("NAS"), "version": ""})
            answer = route("POST", {"links": links("Printer"), "version": opened["version"]})
            self.assertEqual(409, answer.code)
            self.assertEqual(["NAS"], self.titles(kube))

    def test_links_removed_since_the_editor_opened_are_not_brought_back(self):
        with Kube(portal_map(links("Gateway"))) as kube:
            opened = route("GET")
            K.send("DELETE", MAP)
            answer = route("POST", {"links": links("Gateway", "NAS"), "version": opened["version"]})
            self.assertEqual(409, answer.code)
            self.assertIsNone(kube.object(MAP))

    def test_a_save_that_names_no_version_replaces_the_links_as_before(self):
        with Kube(portal_map(links("Gateway"))) as kube:
            route("POST", {"links": links("NAS")})
            self.assertEqual(["NAS"], self.titles(kube))
        with Kube() as kube:
            route("POST", {"links": links("NAS")})
            self.assertEqual(["NAS"], self.titles(kube))


if __name__ == "__main__":
    unittest.main()
