"""Text Homestead writes for people names who did something in words, as the
page does with UI.who: "alice (via Office)", "ada approving Claude Code", not the
stored "fleet:office:alice" or "approving:Claude Code:ada" (#595 wrap-up gap 16).
The cases are shared with tests/who-label.test.js, so the two stay the same."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tests"))

import homestead_agent_requests as requests
import homestead_fleet as fleet

TABLE = json.loads((ROOT / "tests" / "fixtures" / "who-cases.json").read_text(encoding="utf-8"))


class WordsTests(unittest.TestCase):
    def test_every_shared_case_reads_as_the_page_reads_it(self):
        with mock.patch.object(fleet, "members", lambda: TABLE["members"]):
            for stored, words in TABLE["cases"]:
                with self.subTest(stored=stored):
                    self.assertEqual(words, fleet.words(stored))

    def test_an_unreadable_member_list_falls_back_to_the_handle(self):
        def broken():
            raise ValueError("fleet state could not be read")
        with mock.patch.object(fleet, "members", broken):
            self.assertEqual("alice (via office)", fleet.words("fleet:office:alice"))

    def test_a_deeply_nested_name_is_read_without_failing(self):
        # A linked Homestead could send a name nested thousands deep; reading
        # stops after three levels and shows the rest as recorded.
        nested = "approving:a:" * 5000 + "ada"
        with mock.patch.object(fleet, "members", lambda: TABLE["members"]):
            text = fleet.words(nested)
        self.assertIsInstance(text, str)
        self.assertTrue(text.endswith("approving a approving a approving a"))

    def test_malformed_names_still_give_text(self):
        with mock.patch.object(fleet, "members", lambda: TABLE["members"]):
            for name in ("approving:", "approving::", "fleet:", "fleet::", ":", "api-key:", None, 42):
                with self.subTest(name=name):
                    self.assertIsInstance(fleet.words(name), str)

    def test_reading_never_raises_into_the_text_it_is_for(self):
        with mock.patch.object(fleet, "_words", side_effect=RuntimeError("bug")):
            self.assertEqual("fleet:office:alice", fleet.words("fleet:office:alice"))


class AgentRequestTextTests(unittest.TestCase):
    """The request's detail and the audit's detail are text for people; the
    audit's approved_by stays the stored identity, for filtering."""

    def decide(self, how, user):
        ended, audited = {}, []
        row = {"id": "r1", "key": {"id": "k1", "name": "Claude Code"}, "tool": "list_things", "args": {},
               "addr": "192.0.2.31", "digest": ""}
        tool = mock.Mock(kind="read", run=lambda args, ctx: "ok")
        with mock.patch.object(fleet, "members", lambda: TABLE["members"]), \
                mock.patch.object(requests, "_take", lambda rid, user, role, status: dict(row)), \
                mock.patch.object(requests, "_end", lambda rid, **fields: ended.update(fields) or fields), \
                mock.patch.object(requests.AUDIT, "record", lambda *a, **k: audited.append(k)), \
                mock.patch.object(requests, "public", lambda r: r), \
                mock.patch.dict(requests.TOOLS.BY_NAME, {"list_things": tool}), \
                mock.patch.object(requests.TOOLS, "clip", lambda result: (result, False)):
            getattr(requests, how)("r1", user, "admin")
        return ended, audited[0]

    def test_a_denial_names_a_relayed_person_in_words(self):
        ended, audit = self.decide("deny", "fleet:office:alice")
        self.assertEqual("Denied by alice (via Office)", ended["detail"])
        self.assertEqual("denied by alice (via Office)", audit["detail"])
        self.assertEqual("fleet:office:alice", audit["approved_by"])

    def test_an_approval_names_a_relayed_person_in_words(self):
        ended, audit = self.decide("approve", "fleet:office:alice")
        self.assertEqual("Approved by alice (via Office)", ended["detail"])
        self.assertEqual("approved by alice (via Office)", audit["detail"])
        self.assertEqual("fleet:office:alice", audit["approved_by"])

    def test_a_local_name_is_written_as_it_is(self):
        ended, _ = self.decide("deny", "ada")
        self.assertEqual("Denied by ada", ended["detail"])

    def test_a_deeply_nested_name_does_not_stop_the_decision_being_written(self):
        ended, audit = self.decide("deny", "approving:a:" * 5000 + "ada")
        self.assertTrue(ended["detail"].startswith("Denied by "))
        self.assertTrue(audit["detail"].startswith("denied by "))


if __name__ == "__main__":
    unittest.main()
