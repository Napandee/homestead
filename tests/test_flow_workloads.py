"""Containers and scheduled jobs on the architecture page: one card per app,
named after what runs it, and nightly jobs in a lane of their own."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server


def pod(name, ns="lab", labels=None, owner=None, phase="Running", node="node1", claims=()):
    meta = {"name": name, "namespace": ns, "labels": labels or {}}
    if owner:
        meta["ownerReferences"] = [{"kind": owner[0], "name": owner[1], "controller": True}]
    return {"metadata": meta,
            "spec": {"nodeName": node, "containers": [{"name": "c", "image": "x"}],
                     "volumes": [{"name": c, "persistentVolumeClaim": {"claimName": c}} for c in claims]},
            "status": {"phase": phase, "startTime": "2026-10-01T10:00:00Z"}}


def service(name, ns, app, port, ip):
    return {"metadata": {"name": name, "namespace": ns}, "spec": {"selector": {"app": app}, "ports": [{"port": port}]},
            "status": {"loadBalancer": {"ingress": [{"ip": ip}]}}}


def cronjob(name, ns="lab", last="", ok="", active=0, claims=(), **spec):
    return {"metadata": {"name": name, "namespace": ns},
            "spec": {"schedule": "0 2 * * *", **spec,
                     "jobTemplate": {"spec": {"template": {"spec": {"volumes": [
                         {"name": c, "persistentVolumeClaim": {"claimName": c}} for c in claims]}}}}},
            "status": {"lastScheduleTime": last, "lastSuccessfulTime": ok, "active": [{}] * active}}


API = {
    "/api/v1/pods": {"items": [
        # A rollout: the new pod is still pending, the old one runs.
        pod("web-7f8d9-new01", labels={"app": "web", "pod-template-hash": "7f8d9"},
            owner=("ReplicaSet", "web-7f8d9"), phase="Pending", node="node2"),
        pod("web-5c6b7-old01", labels={"app": "web", "pod-template-hash": "5c6b7"},
            owner=("ReplicaSet", "web-5c6b7"), node="node1", claims=["web-data"]),
        pod("shared-postgres-0", owner=("StatefulSet", "shared-postgres")),
        pod("web-6b5a4-other", ns="staging", labels={"app": "web", "pod-template-hash": "6b5a4"},
            owner=("ReplicaSet", "web-6b5a4")),
        # Last night's backups: one finished, one failed, both kept as history.
        pod("backup-web-29330520-abcde", labels={"app": "backup"}, owner=("Job", "backup-web-29330520"),
            phase="Succeeded", node="node2", claims=["web-data"]),
        pod("backup-db-29330520-fghij", owner=("Job", "backup-db-29330520"), phase="Failed"),
        # A pod nothing owns is still shown, by its app label.
        pod("debug", labels={"app": "toolbox"}),
    ]},
    "/api/v1/services": {"items": [service("web", "lab", "web", 80, "192.0.2.10"),
                                   service("web", "staging", "web", 8080, "192.0.2.11")]},
    "/apis/longhorn.io/v1beta2/volumes": {"items": []},
    "/apis/longhorn.io/v1beta2/replicas": {"items": []},
    "/apis/kubevirt.io/v1/virtualmachineinstances": {"items": []},
    "/apis/kubevirt.io/v1/virtualmachines": {"items": []},
    "/apis/apps/v1/deployments": {"items": []},
    "/apis/metrics.k8s.io/v1beta1/pods": {"items": []},
    "/apis/batch/v1/cronjobs": {"items": [
        cronjob("backup-web", last="2026-10-10T02:00:00Z", ok="2026-10-10T02:00:41Z", claims=["web-data"]),
        cronjob("backup-db", last="2026-10-10T02:00:00Z", ok="2026-10-09T02:00:38Z"),
        cronjob("backup-paused", suspend=True, timeZone="Europe/London"),
        cronjob("k3s-housekeeping", ns="kube-system", last="2026-10-10T02:00:00Z"),
    ]},
}


class FlowWorkloadTests(unittest.TestCase):
    def setUp(self):
        patches = [mock.patch.object(server, "kget", side_effect=lambda path: API[path]),
                   mock.patch.object(server.HW, "workload_features", return_value=[])]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.flow = server.get_flow2()
        self.by = {w["id"]: w for w in self.flow["workloads"]}
        self.jobs = {j["id"]: j for j in self.flow["jobs"]}

    def test_finished_job_pods_are_not_containers(self):
        self.assertFalse([w for w in self.by if "backup" in w])

    def test_each_controller_is_one_card_named_after_it(self):
        self.assertEqual({"w:lab/web", "w:lab/shared-postgres", "w:staging/web", "w:lab/toolbox"}, set(self.by))
        self.assertEqual("shared-postgres", self.by["w:lab/shared-postgres"]["name"])

    def test_a_card_shows_the_node_its_app_runs_on_during_a_rollout(self):
        self.assertEqual(("node1", "Running"), (self.by["w:lab/web"]["node"], self.by["w:lab/web"]["phase"]))

    def test_same_named_apps_in_two_namespaces_keep_their_own_ports(self):
        self.assertEqual([80], [p["port"] for p in self.by["w:lab/web"]["ports"]])
        self.assertEqual([8080], [p["port"] for p in self.by["w:staging/web"]["ports"]])

    def test_a_failed_last_run_is_reported_and_a_good_one_is_not(self):
        self.assertTrue(self.jobs["j:lab/backup-db"]["last_failed"])
        self.assertFalse(self.jobs["j:lab/backup-web"]["last_failed"])

    def test_a_job_lists_the_volumes_it_uses(self):
        self.assertEqual(["web-data"], self.jobs["j:lab/backup-web"]["claims"])

    def test_a_paused_job_keeps_its_own_time_zone(self):
        paused = self.jobs["j:lab/backup-paused"]
        self.assertEqual((True, "Europe/London", False), (paused["suspended"], paused["time_zone"], paused["last_failed"]))

    def test_system_namespace_jobs_are_not_listed(self):
        self.assertNotIn("j:kube-system/k3s-housekeeping", self.jobs)

    def test_unreadable_cronjobs_leave_the_page_without_a_jobs_lane(self):
        def kget(path):
            if path == "/apis/batch/v1/cronjobs":
                raise RuntimeError("forbidden")
            return API[path]
        with mock.patch.object(server, "kget", side_effect=kget):
            self.assertEqual([], server.get_flow2()["jobs"])


if __name__ == "__main__":
    unittest.main()
