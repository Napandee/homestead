"""An account name never reads as a power schedule or an API key.

The change history and REQUEST.user name a power schedule's stop or start
"schedule" and a write through an API key "api-key:<name>". An account named
like either would be indistinguishable, so making one is refused.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import homestead_auth as auth
import homestead_changes as changes


class ReservedUsernameTests(unittest.TestCase):
    def setUp(self):
        self.store = {"users": {"owner": {"id": "owner-id", "ver": 1, "role": "admin", "salt": "YQ==", "hash": "hash"}},
                      "signing_key": "k" * 48}
        mock.patch.object(auth, "_load", side_effect=lambda **kw: self.store).start()
        self.save = mock.patch.object(auth, "_save").start()
        mock.patch.object(auth, "_hash", return_value="hash").start()
        self.addCleanup(mock.patch.stopall)

    def assertRefused(self, username, **kwargs):
        with self.assertRaises(ValueError):
            auth.create_user(username, "long-fixture-password", **kwargs)
        self.assertEqual(["owner"], list(self.store["users"]))
        self.save.assert_not_called()

    def test_the_power_schedules_name_is_refused(self):
        self.assertRefused(changes.BY_SCHEDULE)

    def test_the_name_is_refused_however_it_is_typed(self):
        self.assertRefused("  Schedule ")

    def test_an_api_key_name_is_refused(self):
        self.assertRefused("api-key:backup")

    def test_any_colon_is_refused_so_no_name_can_pass_for_an_api_key(self):
        self.assertRefused("ops:night")

    def test_the_message_says_why_without_echoing_the_name(self):
        with self.assertRaises(ValueError) as made:
            auth.create_user("schedule", "long-fixture-password")
        self.assertIn("power schedules", str(made.exception))
        with self.assertRaises(ValueError) as made:
            auth.create_user("ops:night", "long-fixture-password")
        self.assertEqual("a username cannot contain ':'", str(made.exception))
        self.assertNotIn("ops:night", str(made.exception))

    def test_first_run_setup_refuses_it_too(self):
        self.store["users"].clear()
        with self.assertRaises(ValueError):
            auth.create_user("schedule", "long-fixture-password", first_only=True)
        self.assertEqual({}, self.store["users"])

    def test_names_that_only_resemble_them_are_still_fine(self):
        for name in ("scheduler", "api-keys", "my-schedule"):
            auth.create_user(name, "long-fixture-password")
        self.assertEqual({"owner", "scheduler", "api-keys", "my-schedule"}, set(self.store["users"]))

    def test_email_style_names_are_still_fine(self):
        auth.create_user("alice@example.com", "long-fixture-password")
        self.assertIn("alice@example.com", self.store["users"])


if __name__ == "__main__":
    unittest.main()
