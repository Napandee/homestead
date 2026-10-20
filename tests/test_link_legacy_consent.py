"""Linking a cluster added before linking passes the person's consent for a
plain http:// address through to the link, and nothing more (#602)."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_move as MOVE


class LinkLegacyConsentTests(unittest.TestCase):
    def link(self, **kwargs):
        fleet = mock.Mock()
        fleet.others.return_value = []
        fleet.join.return_value = {"member": {"id": "m1", "name": "old"}, "missed": []}
        row = {"name": "old", "url": "http://192.0.2.9:8088", "user": "admin"}
        with mock.patch.object(MOVE, "FLEET", fleet), \
                mock.patch.object(MOVE, "_stored_clusters", return_value=[row]), \
                mock.patch.object(MOVE, "_update_clusters"), \
                mock.patch.object(MOVE, "_password", return_value="secret"), \
                mock.patch.object(MOVE, "ksend"):
            MOVE.link_legacy("old", "https://198.51.100.5:8443", **kwargs)
        return fleet.join.call_args

    def test_the_consent_reaches_the_link(self):
        self.assertIs(True, self.link(plain_ok=True).kwargs.get("plain_ok"))

    def test_without_it_none_is_claimed(self):
        self.assertIs(False, self.link().kwargs.get("plain_ok"))


if __name__ == "__main__":
    unittest.main()
