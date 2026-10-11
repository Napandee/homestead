"""If a host fails, is there room for what it runs: placed by reservation and
tier, critical first, low waiting and evicted before anything else."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_capacity as capacity
import server


def node(name, mem="15Gi", cpu="8", ready=True, cordoned=False):
    return {"metadata": {"name": name}, "spec": {"unschedulable": cordoned},
            "status": {"allocatable": {"cpu": cpu, "memory": mem},
                       "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


def pod(app, host, mem="1Gi", cpu="100m", tier="", ns="lab", kind="ReplicaSet", phase="Running"):
    spec = {"nodeName": host, "containers": [{"name": "c", "resources": {"requests": {"memory": mem, "cpu": cpu}} if mem or cpu else {}}]}
    if tier:
        spec["priorityClassName"] = f"homestead-{tier}"
    owner = f"{app}-abc" if kind == "ReplicaSet" else app
    return {"metadata": {"name": f"{app}-abc-1", "namespace": ns, "ownerReferences": [{"kind": kind, "name": owner, "controller": True}],
                         "labels": {"pod-template-hash": "abc"}},
            "spec": spec, "status": {"phase": phase}}


def preview(*apps, to=("b", "c"), outcome="moves"):
    return {"apps": [{"kind": "app", "ns": "lab", "name": a, "outcome": outcome, "to": list(to)} for a in apps]}


def run(failed, nodes, pods, impact):
    return capacity.forecast(failed, nodes, pods, impact, server.FLOW._flow_owner)


class ForecastTests(unittest.TestCase):
    def test_an_app_goes_to_the_host_with_most_room_and_the_sentence_says_how_full(self):
        nodes = [node("a"), node("b"), node("c")]
        pods = [pod("web", "a", mem="4Gi"), pod("db", "b", mem="6Gi")]
        f = run("a", nodes, pods, preview("web"))
        hosts = {h["name"]: h for h in f["hosts"]}
        self.assertEqual((6.0, 4.0), (hosts["b"]["mem_gib"], hosts["c"]["mem_gib"]))
        self.assertEqual((6.0, 0.0), (hosts["b"]["mem_before_gib"], hosts["c"]["mem_before_gib"]))
        self.assertEqual("fits", f["verdict"])
        # The host it lands on is the one named: b is fuller, but nothing moved to it.
        self.assertEqual("If a fails, c would have 4 of 15 GiB of memory reserved. It fits.", f["sentence"])

    def test_critical_apps_fit_first_and_low_ones_wait_when_the_room_runs_out(self):
        nodes = [node("a"), node("b")]
        pods = [pod("base", "b", mem="8Gi"), pod("ha", "a", mem="5Gi", tier="critical"),
                pod("runner", "a", mem="3Gi", tier="low"), pod("scan", "a", mem="2Gi", tier="low")]
        f = run("a", nodes, pods, preview("ha", "runner", "scan", to=("b",)))
        self.assertEqual("waits", f["verdict"])
        self.assertEqual({"lab/runner"}, {w["id"] for w in f["waits"]})
        self.assertEqual(15.0, {h["name"]: h for h in f["hosts"]}["b"]["mem_gib"])
        self.assertIn("1 low-tier app would wait", f["sentence"])

    def test_a_low_app_already_there_is_evicted_to_fit_a_critical_one(self):
        nodes = [node("a"), node("b")]
        pods = [pod("base", "b", mem="9Gi"), pod("batch", "b", mem="4Gi", tier="low"), pod("ha", "a", mem="5Gi", tier="critical")]
        f = run("a", nodes, pods, preview("ha", to=("b",)))
        self.assertEqual([], f["waits"])
        self.assertEqual([("lab/batch", "b")], [(e["id"], e["on"]) for e in f["evicted"]])

    def test_a_normal_app_waits_when_even_evicting_low_ones_leaves_no_room(self):
        nodes = [node("a", mem="4Gi"), node("b", mem="4Gi")]
        pods = [pod("big", "a", mem="5Gi")]
        f = run("a", nodes, pods, preview("big", to=("b",)))
        self.assertIn("even with lower-tier apps evicted", f["waits"][0]["why"])

    def test_apps_that_stop_anyway_and_daemonset_pods_do_not_move(self):
        nodes = [node("a"), node("b")]
        pods = [pod("pinned", "a", mem="10Gi"), pod("probe", "a", mem="10Gi", kind="DaemonSet"),
                pod("old", "a", mem="10Gi", phase="Succeeded")]
        f = run("a", nodes, pods, {"apps": [{"kind": "app", "ns": "lab", "name": "pinned", "outcome": "stops"}]})
        self.assertEqual((0, "fits"), (f["moving"], f["verdict"]))

    def test_an_app_may_only_go_where_the_preview_allows(self):
        nodes = [node("a"), node("b"), node("c", mem="1Gi")]
        f = run("a", nodes, [pod("coral", "a", mem="2Gi")], preview("coral", to=("c",)))
        self.assertEqual(["lab/coral"], [w["id"] for w in f["waits"]])

    def test_apps_reserving_nothing_are_counted_since_the_figures_are_then_a_floor(self):
        f = run("a", [node("a"), node("b")], [pod("free", "a", mem="", cpu="")], preview("free", to=("b",)))
        self.assertEqual(1, f["unreserved"])

    def test_a_nearly_full_host_is_tight_and_no_host_left_says_so(self):
        f = run("a", [node("a"), node("b")], [pod("x", "a", mem="14Gi")], preview("x", to=("b",)))
        self.assertEqual("tight", f["verdict"])
        self.assertIn("Tight, but it fits", f["sentence"])
        lone = run("a", [node("a"), node("b", cordoned=True)], [pod("x", "a")], preview("x", to=("b",)))
        self.assertEqual("no-host", lone["verdict"])

    def test_a_host_with_nothing_to_move_says_so(self):
        f = run("a", [node("a"), node("b")], [pod("web", "b")], preview())
        self.assertEqual((0, "fits"), (f["moving"], f["verdict"]))
        self.assertEqual("If a fails, nothing on it needs room on another host.", f["sentence"])

    def test_a_cpu_bound_host_is_described_by_its_cpu(self):
        f = run("a", [node("a"), node("b", cpu="2")], [pod("enc", "a", mem="1Gi", cpu="1900m")], preview("enc", to=("b",)))
        self.assertEqual("If a fails, b would have 95% of its CPU reserved. Tight, but it fits.", f["sentence"])

    def test_the_sentence_names_the_tier_and_eviction(self):
        nodes = [node("a"), node("b")]
        pods = [pod("base", "b", mem="9Gi"), pod("batch", "b", mem="4Gi", tier="low"), pod("ha", "a", mem="5Gi", tier="critical")]
        f = run("a", nodes, pods, preview("ha", to=("b",)))
        self.assertIn("1 low-tier app would wait for room or be evicted", f["sentence"])

    def test_vms_move_and_apps_waiting_for_their_host_need_no_room(self):
        launcher = pod("win", "a", mem="8Gi", kind="VirtualMachineInstance")
        f = run("a", [node("a"), node("b")], [launcher, pod("nvr", "a", mem="8Gi")],
                {"apps": [{"kind": "vm", "ns": "lab", "name": "win", "outcome": "moves", "to": ["b"]},
                          {"kind": "app", "ns": "lab", "name": "nvr", "outcome": "waits", "to": ["b"]}]})
        self.assertEqual(1, f["moving"])
        self.assertEqual(8.0, f["hosts"][0]["mem_gib"])

    def test_pods_the_preview_does_not_list_still_take_room(self):
        db = pod("postgres", "a", mem="6Gi", kind="StatefulSet")
        f = run("a", [node("a"), node("b")], [db], preview())
        self.assertEqual((1, 6.0), (f["moving"], f["hosts"][0]["mem_gib"]))

    def test_an_app_goes_where_there_is_room_before_anything_is_evicted(self):
        # b is full of low apps, c is empty: the scheduler picks c and evicts nothing.
        nodes = [node("a"), node("b", mem="16Gi"), node("c", mem="8Gi")]
        pods = [pod("batch", "b", mem="14Gi", tier="low"), pod("web", "a", mem="4Gi")]
        f = run("a", nodes, pods, preview("web"))
        self.assertEqual(("fits", [], []), (f["verdict"], f["waits"], f["evicted"]))
        self.assertEqual(4.0, {h["name"]: h for h in f["hosts"]}["c"]["mem_gib"])

    def test_a_critical_app_evicts_a_normal_one_when_it_must(self):
        nodes = [node("a"), node("b", mem="10Gi")]
        pods = [pod("media", "b", mem="8Gi"), pod("ha", "a", mem="5Gi", tier="critical")]
        f = run("a", nodes, pods, preview("ha", to=("b",)))
        self.assertEqual([], f["waits"])
        self.assertEqual([("lab/media", "normal", "b")], [(e["id"], e["tier"], e["on"]) for e in f["evicted"]])

    def test_a_normal_app_does_not_evict_another_normal_one(self):
        nodes = [node("a"), node("b", mem="10Gi")]
        pods = [pod("media", "b", mem="8Gi"), pod("web", "a", mem="5Gi")]
        f = run("a", nodes, pods, preview("web", to=("b",)))
        self.assertEqual(([], ["lab/web"]), (f["evicted"], [w["id"] for w in f["waits"]]))

    def test_an_unlisted_pod_is_not_placed_on_a_host_whose_taint_it_does_not_tolerate(self):
        pi = node("pi", mem="4Gi")
        pi["spec"]["taints"] = [{"key": "homestead.io/role", "value": "tiebreaker", "effect": "NoSchedule"}]
        nodes = [node("a"), node("b", mem="2Gi"), pi]
        f = run("a", nodes, [pod("postgres", "a", mem="3Gi", kind="StatefulSet")], preview())
        self.assertEqual("waits", f["verdict"])
        self.assertEqual(0.0, {h["name"]: h for h in f["hosts"]}["pi"]["mem_gib"])

    def test_a_bare_pod_does_not_move(self):
        bare = pod("debug", "a", mem="2Gi")
        bare["metadata"].pop("ownerReferences")
        self.assertEqual(0, run("a", [node("a"), node("b")], [bare], preview())["moving"])

    def test_a_host_full_before_anything_moved_does_not_make_it_tight(self):
        nodes = [node("a"), node("b"), node("pi", mem="4Gi")]
        pods = [pod("system", "pi", mem="3800Mi"), pod("web", "a", mem="1Gi")]
        f = run("a", nodes, pods, preview("web", to=("b",)))
        self.assertEqual("fits", f["verdict"])
        self.assertIn("b would have", f["sentence"])

    def test_copies_of_one_app_count_once(self):
        pods = [pod("web", "a", mem="1Gi"), {**pod("web", "a", mem="1Gi"), "metadata": {
            **pod("web", "a")["metadata"], "name": "web-abc-2"}}]
        nodes = [node("a"), node("b", mem="1Gi")]
        f = run("a", nodes, pods, preview("web", to=("b",)))
        self.assertEqual((1, 1), (f["moving"], len(f["waits"])))
        self.assertIn("1 normal-tier app would wait", f["sentence"])

    def test_pending_pods_are_counted_apart(self):
        waiting = pod("big", None, mem="40Gi", phase="Pending")
        f = run("a", [node("a"), node("b")], [waiting], preview())
        self.assertEqual((1, 0), (f["pending"], f["moving"]))


class EveryHostTests(unittest.TestCase):
    def setUp(self):
        bound = (capacity.read, capacity.impact, capacity.sources, capacity.owner, capacity.helper)
        self.addCleanup(lambda: capacity.bind(*bound))

    def bind(self, nodes, pods, impact):
        self.sources = mock.Mock(return_value=([], []))
        capacity.bind(lambda p: {"items": nodes} if p == "/api/v1/nodes" else {"items": pods}, impact, self.sources,
                      server.FLOW._flow_owner, lambda pod: (pod["metadata"].get("labels") or {}).get("homestead.io/task"))

    def test_each_ready_host_is_forecast_and_helper_pods_are_left_out(self):
        helper = pod("files", "a", mem="10Gi")
        helper["metadata"]["labels"]["homestead.io/task"] = "files"
        self.bind([node("a"), node("b"), node("down", ready=False)], [pod("web", "a"), helper],
                  lambda n, src: preview("web", "files", to=("b",)))
        found = capacity.every_host()
        self.assertEqual(["a", "b"], [h["node"] for h in found["hosts"]])
        self.assertEqual(1, found["hosts"][0]["moving"])
        self.sources.assert_called_once()

    def test_one_host_failing_to_preview_leaves_the_others(self):
        def impact(name, src):
            if name == "a":
                raise RuntimeError("boom")
            return preview()
        self.bind([node("a"), node("b")], [], impact)
        found = capacity.every_host()
        self.assertEqual(["unknown", "fits"], [h["verdict"] for h in found["hosts"]])
        self.assertIn("boom", found["hosts"][0]["sentence"])

    def test_the_route_is_for_viewers_and_kept_a_minute(self):
        import homestead_routes as ROUTES
        role, handler = capacity.ROUTES[("GET", "/api/capacity/forecast")]
        kept = []
        with mock.patch.object(ROUTES, "cached", side_effect=lambda key, seconds, fn: kept.append((key, seconds)) or {}):
            handler(ROUTES.Request("GET", "/api/capacity/forecast", {}, None, "ada", "viewer"))
        self.assertEqual(("viewer", [("capacity-forecast", 60)]), (role, kept))


if __name__ == "__main__":
    unittest.main()
