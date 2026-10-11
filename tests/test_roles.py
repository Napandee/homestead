"""Which hosts are tiebreakers: kept for an etcd vote, running no apps (#364)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_roles as ROLES


def api_node(name, labels=None, taints=None):
    return {"metadata": {"name": name, "labels": labels or {}}, "spec": {"taints": taints or []}}


class TiebreakerTests(unittest.TestCase):
    def test_the_label_makes_a_tiebreaker(self):
        self.assertTrue(ROLES.is_tiebreaker(api_node("pi", labels={"node-role/tiebreaker": "true"})))

    def test_the_hard_taint_makes_a_tiebreaker_whatever_its_value(self):
        for effect in ("NoSchedule", "NoExecute"):
            taint = {"key": "node-role/tiebreaker", "value": "yes", "effect": effect}
            self.assertTrue(ROLES.is_tiebreaker(api_node("pi", taints=[taint])), effect)

    def test_a_soft_taint_or_another_key_does_not(self):
        soft = {"key": "node-role/tiebreaker", "value": "true", "effect": "PreferNoSchedule"}
        other = {"key": "gpu", "value": "true", "effect": "NoSchedule"}
        self.assertFalse(ROLES.is_tiebreaker(api_node("a", taints=[soft])))
        self.assertFalse(ROLES.is_tiebreaker(api_node("b", taints=[other])))
        self.assertFalse(ROLES.is_tiebreaker(api_node("c", labels={"node-role/tiebreaker": "false"})))

    def test_homesteads_node_summary_is_read_too(self):
        summary = {"name": "pi", "labels": {}, "taints": [dict(ROLES.TAINT)]}
        self.assertTrue(ROLES.is_tiebreaker(summary))
        self.assertEqual({"pi"}, ROLES.tiebreakers([summary, {"name": "a", "labels": {}, "taints": []}]))

    def test_the_names_match_what_the_join_command_sets(self):
        self.assertEqual({"key": "node-role/tiebreaker", "value": "true", "effect": "NoSchedule"}, ROLES.TAINT)
        self.assertEqual({"node-role/tiebreaker": "true"}, ROLES.LABEL)


if __name__ == "__main__":
    unittest.main()
