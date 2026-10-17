"""Every alert's Review link opens a page that exists, at the card it names.

/storage and /nodes/<host> once landed on the Dashboard, and a #card was
dropped on the way, so Review opened the right page at its top."""
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import server
import homestead_forecast as FORECAST
import homestead_host_os as HOST_OS


# "href": "/x" and "href": f"/x?{y}#z"; links made by a helper are tested below.
HREF = re.compile(r'''"href":\s*f?"(/[^"]*)"''')


def server_links():
    for path in sorted((ROOT / "server").glob("*.py")):
        for link in HREF.findall(path.read_text(encoding="utf-8")):
            yield path.name, link


def page_ids():
    ids = set()
    for path in (ROOT / "web" / "js").glob("*.js"):
        text = path.read_text(encoding="utf-8")
        ids.update(re.findall(r'''\bid[:=]\s*[`"']([A-Za-z][\w-]*)[`"']''', text))
        ids.update(re.findall(r'''\bid="([A-Za-z][\w-]*)"''', text))
    return ids


class AlertLinkTests(unittest.TestCase):
    def test_every_link_names_a_page(self):
        checked = 0
        for name, link in server_links():
            path = re.split(r"[?#]", link, maxsplit=1)[0]
            if "{" in path or path.startswith("/api/"):
                continue
            checked += 1
            self.assertIn(path, server.WEB_PATHS.SPA_ROUTES, f"{name}: {link}")
        self.assertGreater(checked, 10)

    def test_every_card_a_link_names_is_drawn(self):
        ids = page_ids()
        links = list(server_links()) + [("homestead_host_os.py", HOST_OS.updates_href("node-a"))]
        anchors = {(name, link.split("#", 1)[1]) for name, link in links if "#" in link}
        self.assertTrue(anchors)
        for name, anchor in anchors:
            self.assertIn(anchor, ids, f"{name}: #{anchor}")

    def test_host_update_alerts_open_the_host_updates_card(self):
        facts = HOST_OS.alert_facts({"node-a": {"security": 2, "reboot": True,
                                                "auto": {"on": True, "reboots": True}}})
        links = {f["key"].rsplit(":", 1)[1]: f["href"] for f in facts}
        for kind in ("security", "reboot", "autoreboot"):
            self.assertEqual("/settings?tab=updates&find=node-a#settingsHostUpdates", links[kind])

    def test_a_filling_volume_or_disk_opens_where_it_is(self):
        self.assertEqual("/volumes?find=db-data", FORECAST.forecast_href({"kind": "volume", "label": "db-data", "volume": "pvc-1"}))
        self.assertEqual("/nodes?node=node-a&section=storage", FORECAST.forecast_href({"kind": "disk", "node": "node-a"}))
        self.assertEqual("/volumes", FORECAST.forecast_href({"kind": "pool"}))


if __name__ == "__main__":
    unittest.main()
