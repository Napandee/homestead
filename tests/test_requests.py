"""Setting reservations from use for many apps: what is offered, what is left
out and why, the review, and the job that applies it one app at a time."""
import copy
import io
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_requests as reserve


def deployment(name, ns="lab", containers=(("app", {}),), labels=None, generation=1, settled=True):
    return {"metadata": {"name": name, "namespace": ns, "labels": labels or {}, "generation": generation},
            "spec": {"replicas": 1, "template": {"spec": {"containers": [
                {"name": c, "resources": {"requests": dict(r)} if r else {}} for c, r in containers]}}},
            "status": {"observedGeneration": generation if settled else generation - 1,
                       "replicas": 1, "updatedReplicas": 1, "readyReplicas": 1 if settled else 0}}


# What each container's week of use suggests: None when it has too few readings.
SUGGEST = {("lab", "paperless", "app"): ("60m", "480Mi"), ("lab", "frigate", "app"): ("180m", "528Mi"),
           ("lab", "web", "app"): ("20m", "64Mi"), ("lab", "web", "sidecar"): ("10m", "32Mi"),
           ("media", "jellyfin", "app"): ("240m", "1104Mi"), ("homestead", "homestead", "app"): ("100m", "256Mi"),
           ("lab", "unifi", "app"): None}


def usage(ns, name, container, requests=None, limits=None, tier=""):
    found = SUGGEST.get((ns, name, container))
    if not found:
        return {"samples": 96, "why": "A suggestion needs a day of readings; 96 of 288 so far."}
    cpu, memory = found
    return {"days": 7, "samples": 2016, "cpu_p95_m": 40, "mem_p95_mi": 380, "mem_max_mi": 500,
            "suggest": {"cpu": cpu, "memory": memory, "capped": [],
                        "differs": (requests or {}).get("cpu") != cpu or (requests or {}).get("memory") != memory}}


def managed(dep):
    if (dep["metadata"].get("labels") or {}).get("kustomize.toolkit.fluxcd.io/name"):
        return {"by": "Flux", "source": "Kustomization flux-system/apps", "detected": True}
    return {}


class RequestsTests(unittest.TestCase):
    def setUp(self):
        self.deps = {
            "lab/paperless": deployment("paperless"),
            "lab/frigate": deployment("frigate", containers=(("app", {"cpu": "50m", "memory": "128Mi"}),)),
            "lab/web": deployment("web", containers=(("app", {}), ("sidecar", {}))),
            "lab/settled": deployment("settled", containers=(("app", {"cpu": "20m", "memory": "64Mi"}),)),
            "lab/unifi": deployment("unifi"),
            "media/jellyfin": deployment("jellyfin", ns="media", labels={"kustomize.toolkit.fluxcd.io/name": "apps"}),
            "homestead/homestead": deployment("homestead", ns="homestead"),
            "kube-system/coredns": deployment("coredns", ns="kube-system"),
        }
        SUGGEST[("lab", "settled", "app")] = ("20m", "64Mi")
        self.sent = []
        reserve.bind(self.kget, lambda *a, **k: self.sent.append((a, k)), usage, managed,
                     ("homestead", "homestead"), {"kube-system"}, lambda: True)

    def kget(self, path):
        if path == "/apis/apps/v1/deployments":
            return {"items": [copy.deepcopy(d) for d in self.deps.values()]}
        ns, name = path.split("/namespaces/")[1].split("/deployments/")
        if f"{ns}/{name}" not in self.deps:
            raise urllib.error.HTTPError(path, 404, "not found", {}, io.BytesIO())
        return copy.deepcopy(self.deps[f"{ns}/{name}"])

    def test_apps_reserving_nothing_are_ticked_and_others_are_offered_unticked(self):
        plan = reserve.plan()
        chosen = {a["id"]: a["chosen"] for a in plan["apps"]}
        self.assertEqual({"lab/paperless": True, "lab/web": True, "lab/frigate": False}, chosen)
        self.assertEqual(["lab/paperless", "lab/web", "lab/frigate"], [a["id"] for a in plan["apps"]])

    def test_an_app_whose_reservation_matches_its_use_is_not_offered(self):
        self.assertNotIn("lab/settled", {a["id"] for a in reserve.plan()["apps"]})

    def test_a_flux_app_gets_the_yaml_to_commit_instead(self):
        skipped = {s["id"]: s for s in reserve.plan()["skipped"]}
        self.assertIn("Flux applies it", skipped["media/jellyfin"]["why"])
        self.assertIn("cpu: 240m", skipped["media/jellyfin"]["yaml"])
        self.assertIn("memory: 1104Mi", skipped["media/jellyfin"]["yaml"])

    def test_homestead_itself_and_system_namespaces_are_not_changed(self):
        plan = reserve.plan()
        ids = {a["id"] for a in plan["apps"]}
        self.assertFalse({"homestead/homestead", "kube-system/coredns"} & ids)
        self.assertIn("its manifest", {s["id"]: s for s in plan["skipped"]}["homestead/homestead"]["why"])

    def test_an_app_with_too_few_readings_says_why(self):
        self.assertIn("96 of 288", {s["id"]: s for s in reserve.plan()["skipped"]}["lab/unifi"]["why"])

    def test_the_review_token_covers_exactly_the_ticked_apps(self):
        default = reserve.plan()
        more = reserve.plan(["lab/paperless", "lab/web", "lab/frigate"])
        self.assertNotEqual(default["review_token"], more["review_token"])
        chosen = [a for a in more["apps"] if a["chosen"]]
        self.assertEqual(3, len(reserve.reviewed(chosen, more["review_token"])))
        with self.assertRaisesRegex(ValueError, "differ from the review"):
            reserve.reviewed(chosen, default["review_token"])

    def test_a_reservation_set_by_hand_since_the_review_is_not_overwritten(self):
        plan = reserve.plan()
        self.deps["lab/paperless"] = deployment("paperless", containers=(("app", {"memory": "1Gi"}),))
        with self.assertRaisesRegex(ValueError, "changed since the review"):
            reserve.reviewed([a for a in plan["apps"] if a["chosen"]], plan["review_token"])

    def test_an_app_handed_to_flux_since_the_review_is_refused(self):
        plan = reserve.plan()
        self.deps["lab/web"]["metadata"]["labels"] = {"kustomize.toolkit.fluxcd.io/name": "apps"}
        with self.assertRaisesRegex(ValueError, "not one Homestead changes"):
            reserve.reviewed([a for a in plan["apps"] if a["chosen"]], plan["review_token"])

    def job(self, ids):
        plan = reserve.plan(ids)
        apps = reserve.reviewed([a for a in plan["apps"] if a["chosen"]], plan["review_token"])
        return {"ref": {"apps": apps, "index": 0, "done": 0, "stage": "apply"}}

    def test_each_app_restarts_once_with_all_its_containers_and_the_next_waits(self):
        item = self.job(["lab/paperless", "lab/web"])
        self.assertEqual(["lab/paperless", "lab/web"], [a["id"] for a in item["ref"]["apps"]])
        self.deps["lab/paperless"]["status"]["readyReplicas"] = 0      # restarting
        self.assertEqual("running", reserve.status(item)[0])
        reserve.status(item)
        self.assertEqual(1, len(self.sent))                      # web waits for paperless
        self.deps["lab/paperless"]["status"]["readyReplicas"] = 1
        reserve.status(item)
        reserve.status(item)
        (method, path, body), kwargs = self.sent[1]
        self.assertEqual(("PATCH", "/apis/apps/v1/namespaces/lab/deployments/web"), (method, path))
        self.assertEqual(["app", "sidecar"], [c["name"] for c in body["spec"]["template"]["spec"]["containers"]])
        self.assertEqual({"cpu": "10m", "memory": "32Mi"}, body["spec"]["template"]["spec"]["containers"][1]["resources"]["requests"])
        self.assertEqual("application/strategic-merge-patch+json", kwargs["ctype"])
        reserve.status(item)
        state, progress, message = reserve.status(item)
        self.assertEqual(("succeeded", 100), (state, progress))
        self.assertIn("2 apps", message)

    def test_a_refused_change_skips_that_app_and_the_rest_go_on(self):
        item = self.job(["lab/paperless", "lab/web"])
        refuse = urllib.error.HTTPError("x", 422, "request above limit", {}, io.BytesIO())
        reserve.bind(self.kget, mock.Mock(side_effect=[refuse, None]), usage, managed,
                     ("homestead", "homestead"), {"kube-system"}, lambda: True)
        state, _, message = reserve.status(item)
        self.assertEqual("running", state)
        self.assertIn("Skipped lab/paperless", message)
        self.assertEqual(1, item["ref"]["index"])

    def test_an_app_that_does_not_come_back_stops_the_job(self):
        item = self.job(["lab/paperless", "lab/web"])
        self.deps["lab/paperless"]["status"]["readyReplicas"] = 0
        reserve.status(item)
        item["ref"]["started"] -= 601
        state, _, message = reserve.status(item)
        self.assertEqual("failed", state)
        self.assertIn("the rest were not changed", message)
        self.assertEqual(1, len(self.sent))


if __name__ == "__main__":
    unittest.main()
