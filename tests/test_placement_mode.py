"""How hosts share the work: the setting, the standby taint Homestead keeps
in step with it, and the rebalance emptying standby hosts first."""
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_container_rebalance as C
import homestead_placement_mode as mode
import server
from test_container_rebalance import Cluster, bind


def node(name, ready=True, taints=None):
    return {"metadata": {"name": name, "resourceVersion": "7"}, "spec": {"taints": taints or []},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


OURS = dict(mode.TAINT)
THEIRS = {"key": "gpu", "value": "true", "effect": "NoSchedule"}


class SettingTests(unittest.TestCase):
    def test_a_mode_not_chosen_yet_is_allowed_and_standby_needs_a_host(self):
        self.assertEqual({"mode": "", "standby_nodes": []}, mode.validate({}))
        self.assertEqual(["pi"], mode.validate({"mode": "standby", "standby_nodes": ["pi", "pi"]})["standby_nodes"])
        with self.assertRaisesRegex(ValueError, "at least one standby host"):
            mode.validate({"mode": "standby"})
        with self.assertRaisesRegex(ValueError, "default, spread or standby"):
            mode.validate({"mode": "everywhere"})
        with self.assertRaisesRegex(ValueError, "host names"):
            mode.validate({"mode": "standby", "standby_nodes": ["Not A Host!"]})

    def test_settings_keep_the_mode_beside_the_rebalance(self):
        saved = server.validate_app_settings({"placement": {"rebalance": "suggest", "mode": "standby", "standby_nodes": ["pi"]}})
        self.assertEqual({"rebalance": "suggest", "mode": "standby", "standby_nodes": ["pi"]}, saved["placement"])
        self.assertEqual({"rebalance": "off", "mode": "", "standby_nodes": []}, server.validate_app_settings({})["placement"])


class TaintTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        mode.bind(None, lambda method, path, body, ctype: self.sent.append((method, path, body, ctype)))

    def test_a_standby_host_gets_the_soft_taint_and_keeps_its_own(self):
        changed, _ = mode.reconcile([node("a"), node("pi", taints=[THEIRS])], {"mode": "standby", "standby_nodes": ["pi"]})
        self.assertEqual(["pi is now standby"], changed)
        method, path, body, ctype = self.sent[0]
        self.assertEqual(("PATCH", "/api/v1/nodes/pi", "application/merge-patch+json"), (method, path, ctype))
        self.assertEqual([THEIRS, OURS], body["spec"]["taints"])
        self.assertEqual("PreferNoSchedule", OURS["effect"])
        # Refused if the node's taints changed since they were read.
        self.assertEqual({"resourceVersion": "7"}, body["metadata"])

    def test_a_refused_change_is_a_note_and_the_other_hosts_still_change(self):
        def send(method, path, body, ctype):
            if path.endswith("/pi"):
                raise urllib.error.HTTPError(path, 409, "Conflict", {}, None)
            self.sent.append(path)
        mode.bind(None, send)
        changed, notes = mode.reconcile([node("a"), node("pi"), node("nas")],
                                        {"mode": "standby", "standby_nodes": ["pi", "nas"]})
        self.assertEqual(["nas is now standby"], changed)
        self.assertIn("pi was not changed this time", notes[-1])

    def test_leaving_standby_mode_removes_only_homesteads_taint(self):
        changed, _ = mode.reconcile([node("pi", taints=[THEIRS, OURS]), node("bare", taints=[OURS])], {"mode": "spread"})
        self.assertEqual({"pi takes apps again", "bare takes apps again"}, set(changed))
        bodies = {path: body for _, path, body, _ in self.sent}
        self.assertEqual([THEIRS], bodies["/api/v1/nodes/pi"]["spec"]["taints"])
        self.assertIsNone(bodies["/api/v1/nodes/bare"]["spec"]["taints"])

    def test_nothing_is_written_when_the_taints_already_match(self):
        mode.reconcile([node("a"), node("pi", taints=[OURS])], {"mode": "standby", "standby_nodes": ["pi"]})
        self.assertEqual([], self.sent)

    def test_the_last_host_taking_work_is_never_made_standby(self):
        changed, notes = mode.reconcile([node("a", ready=False), node("pi")], {"mode": "standby", "standby_nodes": ["pi"]})
        self.assertEqual([], changed)
        self.assertIn("no other host takes work", notes[0])

    def test_a_named_host_not_in_the_cluster_is_said(self):
        _, notes = mode.reconcile([node("a")], {"mode": "standby", "standby_nodes": ["gone"]})
        self.assertIn("gone is not a host", notes[0])

    def test_the_summary_counts_hosts_and_applies_with_two_taking_work(self):
        found = mode.summary([node("a"), node("pi", taints=[OURS])], {"mode": "standby", "standby_nodes": ["pi"]}, 3)
        self.assertEqual(("standby", 3, True), (found["mode"], found["unreserved"], found["applies"]))
        self.assertEqual([False, True], [h["standby"] for h in found["hosts"]])
        self.assertFalse(mode.summary([node("solo")], {}, 0)["applies"])


class StandbyRebalanceTests(unittest.TestCase):
    def test_apps_on_a_standby_host_move_off_it_and_nothing_moves_onto_one(self):
        c = Cluster()
        bind(c)
        moves = C.plan(avoid=["k3"])["moves"]
        self.assertTrue(moves)
        self.assertNotIn("k3", [m["to"] for m in moves])

    def test_a_standby_host_is_emptied_even_when_it_is_not_the_busiest(self):
        c = Cluster()
        # Everything runs on k1, which is quiet; k2 and k3 have room.
        for p in c.pods:
            p["spec"]["nodeName"] = "k1"
        bind(c)
        moved = C.plan(avoid=["k1"])["moves"]
        self.assertTrue(moved)
        self.assertEqual({"k1"}, {m["from"] for m in moved})

    def test_nothing_moves_off_a_standby_host_onto_one_that_would_be_too_full(self):
        c = Cluster()
        # The spare k3 runs the apps; k1 and k2 are past SPARE_ROOM already.
        for p in c.pods:
            p["spec"]["nodeName"] = "k3"
        usage = {"k1": "15Gi", "k2": "15Gi", "k3": "11Gi"}
        for row in c.objects["/apis/metrics.k8s.io/v1beta1/nodes"]["items"]:
            row["usage"]["memory"] = usage[row["metadata"]["name"]]
        bind(c)
        self.assertEqual([], C.plan(avoid=["k3"])["moves"])


class TickTests(unittest.TestCase):
    def test_each_look_keeps_the_taints_in_step_and_plans_around_standby(self):
        settings = {"placement": {"rebalance": "suggest", "mode": "standby", "standby_nodes": ["pi"]},
                    "updates": {"maintenance": {"days": [], "start": "02:00", "duration_minutes": 60}}}
        with mock.patch.object(server, "cached", side_effect=lambda k, t, fn: settings), \
                mock.patch.object(server, "kget", return_value={"items": [node("a"), node("pi")]}), \
                mock.patch.object(server.PLACEMODE, "reconcile") as reconcile, \
                mock.patch.object(server.RECOVERY, "observe", return_value={"a": 1}), \
                mock.patch.object(server.RECOVERY, "suggest"), \
                mock.patch.object(server.OPS, "list_operations", return_value=[]), \
                mock.patch.object(server.CREBALANCE, "plan", return_value={"moves": [], "excluded": []}) as plan, \
                mock.patch.object(server, "_recovery_planned", [0.0]):
            server.recovery_tick(1_760_000_000)
        reconcile.assert_called_once()
        self.assertEqual(["pi"], plan.call_args.kwargs["avoid"])


if __name__ == "__main__":
    unittest.main()
