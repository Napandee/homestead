"""Whether a pod may be scheduled on a host, as the API gives the host: what
a bulk change weighs room on, so an app pinned to a full host is not told a
host it may not use has room."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_place as PLACE


def node(name, labels=None, taints=None):
    return {"metadata": {"name": name, "labels": {"kubernetes.io/hostname": name, **(labels or {})}},
            "spec": {"taints": taints or []}}


class MayRunOnTests(unittest.TestCase):
    def test_a_pod_with_no_rules_may_run_anywhere_untainted(self):
        self.assertTrue(PLACE.may_run_on({}, node("a")))

    def test_a_node_selector_names_the_hosts_it_may_use(self):
        spec = {"nodeSelector": {"kubernetes.io/hostname": "a"}}
        self.assertEqual((True, False), (PLACE.may_run_on(spec, node("a")), PLACE.may_run_on(spec, node("b"))))

    def test_required_affinity_rules_a_host_out(self):
        spec = {"affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [
            {"matchExpressions": [{"key": "gpu", "operator": "In", "values": ["yes"]}]}]}}}}
        self.assertEqual((True, False), (PLACE.may_run_on(spec, node("a", {"gpu": "yes"})), PLACE.may_run_on(spec, node("b"))))

    def test_a_hard_taint_needs_a_toleration_and_a_soft_one_does_not(self):
        hard = node("pi", taints=[{"key": "homestead.io/role", "value": "tiebreaker", "effect": "NoSchedule"}])
        soft = node("spare", taints=[{"key": "homestead.io/standby", "value": "true", "effect": "PreferNoSchedule"}])
        tolerates = {"tolerations": [{"key": "homestead.io/role", "operator": "Exists"}]}
        self.assertEqual((False, True, True), (PLACE.may_run_on({}, hard), PLACE.may_run_on(tolerates, hard),
                                               PLACE.may_run_on({}, soft)))

    def test_a_pod_given_a_node_name_runs_only_there(self):
        self.assertFalse(PLACE.may_run_on({"nodeName": "a"}, node("b")))


if __name__ == "__main__":
    unittest.main()
