"""Restore into this app (#743): a backup restored into a new claim, swapped in
for the app's own, and swapped back on its own when the app does not come up.
The old claim is kept as a previous volume, which expires."""
import contextlib
import copy
import json
import sys
import types
import unittest
import urllib.error
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_restore_swap as RS
import homestead_routes as MODULE_ROUTES

NS = "lab"
DEP = f"/apis/apps/v1/namespaces/{NS}/deployments/app-1"
OLD = f"/api/v1/namespaces/{NS}/persistentvolumeclaims/app-1-data"
NEW = f"/api/v1/namespaces/{NS}/persistentvolumeclaims/app-1-data-restore"
LISTS = {"deployments", "statefulsets", "daemonsets", "cronjobs", "pods", "persistentvolumeclaims"}
NOW = 1_800_000_000.0


def missing(path):
    return urllib.error.HTTPError(path, 404, "not found", {}, None)


def merge(target, patch):
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def pointer(obj, path):
    parts = [p for p in path.split("/") if p]
    for part in parts[:-1]:
        obj = obj[int(part)] if isinstance(obj, list) else obj[part]
    last = parts[-1]
    return obj, int(last) if isinstance(obj, list) else last


class Kube:
    """Objects by path; lists read from them; pods follow the Deployments."""

    def __init__(self):
        self.objs, self.sent, self.loose, self.ready = {}, [], [], True

    def add(self, path, obj):
        self.objs[path] = obj
        return obj

    def _pods(self, ns):
        pods = []
        for path, dep in self.objs.items():
            if f"/namespaces/{ns}/deployments/" in path and self.ready and dep["spec"].get("replicas", 1):
                pods.append({"metadata": {"name": dep["metadata"]["name"] + "-5d8f-x1"},
                             "spec": copy.deepcopy(dep["spec"]["template"]["spec"]), "status": {"phase": "Running"}})
        return pods + copy.deepcopy(self.loose)

    def get(self, path, **_):
        base, _, query = path.partition("?")
        if base in self.objs:
            return copy.deepcopy(self.objs[base])
        if base.rsplit("/", 1)[-1] in LISTS:
            if base.endswith("/pods"):
                return {"items": self._pods(base.split("/")[4])}
            rows = [copy.deepcopy(o) for p, o in self.objs.items() if p.rsplit("/", 1)[0] == base or (
                base == "/api/v1/persistentvolumeclaims" and "/persistentvolumeclaims/" in p)]
            if "labelSelector=" in query:
                key, value = urllib.parse.unquote(query.split("labelSelector=", 1)[1]).split("=")
                rows = [o for o in rows if (o["metadata"].get("labels") or {}).get(key) == value]
            return {"items": rows}
        raise missing(path)

    def send(self, method, path, body=None, ctype="application/json", **_):
        self.sent.append((method, path, copy.deepcopy(body), ctype))
        obj = self.objs.get(path)
        if obj is None:
            raise missing(path)
        before = json.dumps(obj.get("spec"), sort_keys=True)
        if ctype == "application/json-patch+json":
            trial = copy.deepcopy(obj)
            for op in body:
                parent, key = pointer(trial, op["path"])
                if op["op"] == "test" and parent[key] != op["value"]:
                    raise urllib.error.HTTPError(path, 422, "test failed", {}, None)
                if op["op"] == "replace":
                    parent[key] = op["value"]
            obj.clear(); obj.update(trial)
        else:
            merge(obj, body)
        if "/deployments/" in path:
            if json.dumps(obj.get("spec"), sort_keys=True) != before:
                obj["metadata"]["generation"] += 1
            self.settle(obj)
        return copy.deepcopy(obj)

    def settle(self, dep):
        want = int(dep["spec"].get("replicas", 1))
        n = want if self.ready else 0
        dep["status"] = {"observedGeneration": dep["metadata"]["generation"], "updatedReplicas": n,
                         "readyReplicas": n, "availableReplicas": n, "replicas": want}

    def writes(self, path=None, ctype=None):
        return [s for s in self.sent if (path is None or s[1] == path) and (ctype is None or s[3] == ctype)]


def deployment(name="app-1", claim="app-1-data", kind="Deployment", replicas=1, labels=None):
    obj = {"metadata": {"name": name, "namespace": NS, "generation": 3, "resourceVersion": "100", "labels": labels or {}},
           "spec": {"replicas": replicas, "template": {"spec": {
               "containers": [{"name": "app", "volumeMounts": [{"name": "data", "mountPath": "/config"}]}],
               "volumes": [{"name": "cfg", "configMap": {"name": "app-1"}},
                           {"name": "data", "persistentVolumeClaim": {"claimName": claim}}]}}}}
    obj["kind"] = kind
    return obj


def claim(name, uid, annotations=None, labels=None):
    return {"metadata": {"name": name, "namespace": NS, "uid": uid, "annotations": dict(annotations or {}),
                         "labels": dict(labels or {})},
            "spec": {"volumeName": "pv-" + uid, "storageClassName": "longhorn", "accessModes": ["ReadWriteOnce"],
                     "resources": {"requests": {"storage": "10Gi"}}}}


class FakeOps:
    TERMINAL = {"succeeded", "failed", "cancelled"}

    def __init__(self):
        self.started, self.items = [], []

    def transaction(self):
        return contextlib.nullcontext()

    def start(self, kind, title, resource, href, ref, message="", unless=()):
        self.started.append({"kind": kind, "title": title, "resource": resource, "ref": copy.deepcopy(ref)})
        return {"id": "op1", "kind": kind}

    def snapshot(self):
        return copy.deepcopy(self.items)


class Base(unittest.TestCase):
    def setUp(self):
        self.kube = kube = Kube()
        kube.add(DEP, deployment())
        kube.settle(kube.objs[DEP])
        kube.add(OLD, claim("app-1-data", "u-old"))
        self.ops = FakeOps()
        self.backup = {"metadata": {"name": "backup-0123abcd"},
                       "status": {"state": "Completed", "url": "s3://example.com/b", "volumeSize": str(10 * 1024 ** 3),
                                  "backupCreatedAt": "2026-10-20T03:00:00Z",
                                  "labels": {"KubernetesStatus": json.dumps({"namespace": NS, "pvcName": "app-1-data"})}}}
        self.restored, self.restore_result, self.cancelled = [], ("succeeded", 100, "Restored"), []
        self.uptime, self.settings, self.notes, self.deleted = {}, {"restore_swap": {"keep_days": 7}}, [], []
        self.leader, self.self_app = True, False
        saved = {name: getattr(RS, name) for name in RS.HOOKS}
        self.addCleanup(lambda: [setattr(RS, name, value) for name, value in saved.items()])
        RS.kget, RS.ksend = kube.get, kube.send
        RS.OPS = self.ops
        RS.fence = lambda handle: contextlib.nullcontext()
        RS.backup_object = lambda name: copy.deepcopy(self.backup)
        RS.restore_plan = self.fake_restore_plan
        RS.restore_backup = self.fake_restore
        RS.restore_resolver = lambda item: self.restore_result
        RS.restore_cancel = lambda item: self.cancelled.append(copy.deepcopy(item["ref"])) or "Restore stopped"
        RS.uptime_report = lambda: self.uptime
        RS.settings = lambda: self.settings
        RS.is_self = lambda ns, name: self.self_app
        RS.system_namespaces = lambda: {"kube-system", "longhorn-system"}
        RS.is_leader = lambda: self.leader
        RS.note = self.notes.append
        RS.delete_volume = lambda cfg: self.deleted.append(cfg) or {"ok": True}
        RS.deletion_plan = self.fake_deletion_plan

    def fake_restore_plan(self, backup, ns, name):
        taken = f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}" in self.kube.objs
        return {"minimum_size_gb": 10, "storage_class": "longhorn", "storage_classes": ["longhorn"],
                "conflict": {"message": f"PVC {ns}/{name} already exists; choose a new name"} if taken else None}

    def fake_restore(self, cfg):
        self.restored.append(copy.deepcopy(cfg))
        made = claim(cfg["name"], "u-new", annotations=cfg["annotations"])
        self.kube.add(f"/api/v1/namespaces/{cfg['namespace']}/persistentvolumeclaims/{cfg['name']}", made)
        return {"created": True, "name": cfg["name"], "source_size_bytes": 10 * 1024 ** 3, "message": "Restore started"}

    def fake_deletion_plan(self, ns, name):
        pods = [p for p in self.kube._pods(ns) if any((v.get("persistentVolumeClaim") or {}).get("claimName") == name
                                                      for v in p["spec"].get("volumes") or [])]
        return {"uid": self.kube.objs[f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}"]["metadata"]["uid"],
                "consumers": [{"kind": "Pod", "name": p["metadata"]["name"], "active": True} for p in pods],
                "blocking_reasons": []}

    def plan(self, **extra):
        return RS.plan("backup-0123abcd", **extra)

    def started(self, **extra):
        p = self.plan()
        body = {"backup": "backup-0123abcd", "namespace": NS, "app": "app-1", "claim": "app-1-data",
                "digest": p["digest"], **extra}
        result = RS.start(body)
        ref = self.ops.started[-1]["ref"]
        self.checkpoints = []
        return result, {"id": "op1", "status": "running", "ref": ref}

    def step(self, item, now):
        def checkpoint(it):
            self.checkpoints.append(copy.deepcopy(it))
        return RS.resolve(item, checkpoint, now=now)

    def run_until(self, item, phase, now=NOW, limit=20):
        result = None
        for _ in range(limit):
            if item["ref"].get("phase") == phase:
                return result
            result = self.step(item, now)
            if result[0] != "running":
                return result
        self.fail(f"never reached {phase}: {item['ref'].get('phase')}")

    def template_claim(self):
        return self.kube.objs[DEP]["spec"]["template"]["spec"]["volumes"][1]["persistentVolumeClaim"]["claimName"]

    def marks(self, path):
        meta = self.kube.objs[path]["metadata"]
        return meta.get("labels") or {}, meta.get("annotations") or {}


class PlanTests(Base):
    def test_a_deployment_with_one_claim_is_ready_with_five_steps(self):
        p = self.plan()
        self.assertTrue(p["ready"], p["blockers"])
        self.assertEqual(["restore", "stop", "swap", "start", "watch"], p["steps"])
        self.assertEqual(({"namespace": NS, "name": "app-1", "kind": "Deployment", "replicas": 1}, "app-1-data"),
                         (p["app"], p["claim"]))
        self.assertEqual(("app-1-data-restore", 7, "data"), (p["new_claim"], p["keep_days"], p["mount"]["volume"]))
        self.assertEqual([{"name": "app", "path": "/config"}], p["mount"]["containers"])
        self.assertTrue(p["digest"])

    def blocked(self, needle, **extra):
        p = self.plan(**extra)
        self.assertFalse(p["ready"])
        self.assertTrue(any(needle in b for b in p["blockers"]), p["blockers"])

    def test_a_statefulset_waits(self):
        del self.kube.objs[DEP]
        self.kube.add(f"/apis/apps/v1/namespaces/{NS}/statefulsets/app-1", deployment(kind="StatefulSet"))
        self.blocked("StatefulSet")

    def test_a_claim_shared_with_another_workload(self):
        self.kube.add(f"/apis/apps/v1/namespaces/{NS}/deployments/app-2", deployment("app-2"))
        self.blocked("app-2")

    def test_a_loose_pod(self):
        self.kube.loose.append({"metadata": {"name": "debug"}, "status": {"phase": "Running"},
                                "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "app-1-data"}}]}})
        self.blocked("debug")

    def test_homesteads_own_data(self):
        self.self_app = True
        self.blocked("Homestead's own data")

    def test_a_backup_that_is_not_completed(self):
        self.backup["status"]["state"] = "InProgress"
        self.blocked("not ready to restore")

    def test_an_active_job_on_the_claim(self):
        self.ops.items.append({"kind": "reclass", "status": "running", "resource": {"namespace": NS, "name": "app-1-data"},
                               "ref": {"namespace": NS, "claim": "app-1-data"}})
        self.blocked("already has a job")

    def test_a_new_name_that_is_taken(self):
        self.kube.add(f"/api/v1/namespaces/{NS}/persistentvolumeclaims/taken", claim("taken", "u-t"))
        self.blocked("already exists", new_name="taken")

    def test_a_free_name_is_chosen_when_the_usual_one_is_taken(self):
        self.kube.add(NEW, claim("app-1-data-restore", "u-x"))
        self.assertEqual("app-1-data-restore-2", self.plan()["new_claim"])

    def test_a_flux_app_needs_the_notice_accepted(self):
        self.kube.objs[DEP]["metadata"]["labels"] = {"kustomize.toolkit.fluxcd.io/name": "apps",
                                                     "kustomize.toolkit.fluxcd.io/namespace": "flux-system"}
        p = self.plan()
        self.assertEqual("Flux", p["managed"]["by"])
        self.assertTrue(p["ready"])
        with self.assertRaisesRegex(ValueError, "Flux"):
            self.started()
        self.started(accept_gitops=True)
        self.assertEqual("restore-swap", self.ops.started[-1]["kind"])


class StartTests(Base):
    def test_a_stale_review_is_a_409_with_a_fresh_plan(self):
        p = self.plan()
        self.kube.objs[DEP]["metadata"]["generation"] = 9
        reply = RS.start({"backup": "backup-0123abcd", "namespace": NS, "app": "app-1", "claim": "app-1-data",
                          "digest": p["digest"]})
        self.assertIsInstance(reply, MODULE_ROUTES.Reply)
        self.assertEqual(409, reply.code)
        self.assertNotEqual(p["digest"], reply.body["plan"]["digest"])
        self.assertEqual([], self.restored)

    def test_start_restores_with_the_restore_id_and_records_the_job(self):
        result, item = self.started()
        cfg = self.restored[0]
        self.assertEqual(("app-1-data-restore", NS), (cfg["name"], cfg["namespace"]))
        self.assertEqual(cfg["restore_id"], cfg["annotations"]["homestead.io/restore-id"])
        self.assertEqual("lab/app-1", cfg["annotations"]["homestead.io/restored-into"])
        ref = item["ref"]
        self.assertEqual(("restoring", "restore", "app-1-data", "app-1-data-restore", "u-old"),
                         (ref["phase"], ref["direction"], ref["old_claim"], ref["new_claim"], ref["old_uid"]))
        self.assertEqual("app-1-data-restore", result["name"])

    def test_the_routes_and_their_roles(self):
        import homestead_route_policy as POLICY
        self.assertIn("homestead_restore_swap", MODULE_ROUTES.MODULES)
        for method, path, role in (("GET", "/api/restore-swap/plan", "viewer"), ("POST", "/api/restore-swap", "admin"),
):
            with self.subTest(path=path):
                self.assertEqual(role, POLICY.role(path, method))


class ResolveTests(Base):
    def test_the_happy_path(self):
        _, item = self.started()
        self.run_until(item, "watching")
        ref = item["ref"]
        self.assertEqual(1, ref["replicas_before"])
        scales = [s[2]["spec"]["replicas"] for s in self.kube.writes(DEP, "application/merge-patch+json")]
        self.assertEqual([0, 1], scales)
        swaps = self.kube.writes(DEP, "application/json-patch+json")
        self.assertEqual(1, len(swaps))
        self.assertEqual([{"op": "test", "path": "/metadata/generation", "value": 4},
                          {"op": "test", "path": "/spec/template/spec/volumes/1/persistentVolumeClaim/claimName", "value": "app-1-data"},
                          {"op": "replace", "path": "/spec/template/spec/volumes/1/persistentVolumeClaim/claimName", "value": "app-1-data-restore"}],
                         swaps[0][2])
        labels, notes = self.marks(OLD)
        self.assertEqual("true", labels["homestead.io/previous-volume"])
        self.assertEqual(("lab/app-1", "app-1-data-restore", str(int(NOW)), "op1", "u-old", "restored"),
                         tuple(notes["homestead.io/" + k] for k in ("previous-of", "replaced-by", "replaced-at",
                                                                     "replaced-job", "replaced-uid", "replaced-reason")))
        self.assertEqual("lab/app-1", self.marks(NEW)[1]["homestead.io/restored-into"])
        self.assertEqual(("running", ), self.step(item, NOW + 5)[:1])         # rolled out: waiting for an answer
        self.uptime = {"lab/app-1": {"state": "up", "last": {"at": NOW + 10}}}
        status, _, message = self.step(item, NOW + 20)
        self.assertEqual("succeeded", status)
        self.assertIn("app-1 runs on app-1-data-restore", message)
        self.assertIn("previous volume app-1-data is kept until", message)

    def test_an_app_with_no_check_passes_once_it_stays_ready(self):
        _, item = self.started()
        self.run_until(item, "watching")
        self.step(item, NOW)
        self.assertEqual("running", self.step(item, NOW + 60)[0])
        self.assertEqual("succeeded", self.step(item, NOW + RS.AUTOUPDATE.STEADY + 1)[0])

    def test_resuming_a_step_never_scales_or_swaps_twice(self):
        _, item = self.started()
        self.run_until(item, "watching")
        # A replica that restarted right after the scale to 0, before it was
        # recorded, carries on from the checkpoint written before it.
        stop = next(c for c in self.checkpoints if c["ref"]["phase"] == "stopping" and "replicas_before" in c["ref"])
        again = copy.deepcopy(stop)
        self.run_until(again, "watching")
        self.assertEqual(1, again["ref"]["replicas_before"])
        # One that restarted after the swap was sent, before it was recorded.
        swap = copy.deepcopy(next(c for c in self.checkpoints if c["ref"]["phase"] == "swapping"))
        self.run_until(swap, "watching")
        self.assertEqual(1, len(self.kube.writes(DEP, "application/json-patch+json")))
        self.assertEqual(1, self.kube.objs[DEP]["spec"]["replicas"])
        self.assertEqual(str(int(NOW)), self.marks(OLD)[1]["homestead.io/replaced-at"])

    def test_a_swap_refused_because_the_app_was_edited_changes_nothing(self):
        _, item = self.started()
        send = self.kube.send

        def edited_meanwhile(method, path, body=None, ctype="application/json", **options):
            out = send(method, path, body, ctype)
            if path == DEP and ctype == "application/merge-patch+json" and body["spec"]["replicas"] == 0:
                self.kube.objs[DEP]["metadata"]["generation"] += 1      # someone's edit right after the stop
            return out
        RS.ksend = edited_meanwhile
        status, _, message = self.step(item, NOW)
        self.assertEqual("failed", status)
        self.assertIn("Nothing was changed", message)
        self.assertEqual("app-1-data", self.template_claim())
        self.assertEqual(1, self.kube.objs[DEP]["spec"]["replicas"])
        self.assertEqual("not-used", self.marks(NEW)[1]["homestead.io/replaced-reason"])


class SwapBackTests(Base):
    def swapped_back(self, item, why, now):
        status, _, message = self.step(item, now)
        self.assertEqual("running", status, message)
        self.assertEqual("back-starting", item["ref"]["phase"])
        self.assertEqual("app-1-data", self.template_claim())
        self.assertEqual(1, self.kube.objs[DEP]["spec"]["replicas"])
        old_labels, old_notes = self.marks(OLD)
        self.assertNotIn("homestead.io/previous-volume", old_labels)
        self.assertFalse([k for k in old_notes if k.startswith("homestead.io/replaced")])
        new_labels, new_notes = self.marks(NEW)
        self.assertEqual("true", new_labels["homestead.io/previous-volume"])
        self.assertEqual(("not-started", "u-new"), (new_notes["homestead.io/replaced-reason"], new_notes["homestead.io/replaced-uid"]))
        status, _, message = self.step(item, now + 1)
        self.assertEqual("failed", status)
        self.assertIn("Swapped back: on the restored volume app-1 " + why, message)
        self.assertIn("app-1-data-restore is kept until", message)

    def test_rollout_not_ready_in_time(self):
        _, item = self.started()
        self.kube.ready = False
        self.run_until(item, "watching")
        self.assertEqual("running", self.step(item, NOW + 60)[0])
        self.kube.ready = True
        self.swapped_back(item, "was not ready after 10 minutes", NOW + RS.AUTOUPDATE.ROLLOUT_WAIT + 1)

    def test_pods_stop_being_ready(self):
        _, item = self.started()
        self.run_until(item, "watching")
        self.step(item, NOW)
        self.kube.objs[DEP]["status"]["readyReplicas"] = 0
        self.swapped_back(item, "stopped being ready", NOW + 30)

    def test_the_uptime_check_says_down(self):
        _, item = self.started()
        self.run_until(item, "watching")
        self.step(item, NOW)
        self.uptime = {"lab/app-1": {"state": "down", "last": {"at": NOW + 5, "error": "connection refused"}}}
        self.swapped_back(item, "stopped answering (connection refused)", NOW + 10)

    def test_no_answer_in_time(self):
        _, item = self.started()
        self.run_until(item, "watching")
        self.step(item, NOW)
        self.uptime = {"lab/app-1": {"state": "up", "last": {"at": NOW - 100}}}
        self.swapped_back(item, "did not answer within 5 minutes", NOW + RS.AUTOUPDATE.ANSWER_WAIT + 1)


class BeforeTheSwapTests(Base):
    def test_a_failed_restore_changes_nothing_and_marks_the_claim_it_made(self):
        _, item = self.started()
        self.restore_result = ("failed", 20, "The restored Longhorn volume is faulted")
        status, _, message = self.step(item, NOW)
        self.assertEqual("failed", status)
        self.assertIn("Nothing was changed", message)
        self.assertEqual([], self.kube.writes(DEP))
        self.assertEqual("not-used", self.marks(NEW)[1]["homestead.io/replaced-reason"])

    def test_a_stop_that_times_out_starts_the_app_again(self):
        _, item = self.started()
        self.kube.loose.append({"metadata": {"name": "stuck"}, "status": {"phase": "Running"},
                                "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "app-1-data"}}]}})
        self.run_until(item, "stopping")
        self.assertEqual("running", self.step(item, NOW)[0])
        status, _, message = self.step(item, NOW + RS.STOP_WAIT + 1)
        self.assertEqual("failed", status)
        self.assertIn("Nothing was changed", message)
        self.assertEqual(1, self.kube.objs[DEP]["spec"]["replicas"])
        self.assertEqual("app-1-data", self.template_claim())


class CancelTests(Base):
    def test_before_the_swap_the_restore_stops_and_the_app_starts(self):
        _, item = self.started()
        self.kube.loose.append({"metadata": {"name": "stuck"}, "status": {"phase": "Running"},
                                "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "app-1-data"}}]}})
        self.assertEqual("running", self.step(item, NOW)[0])
        self.assertEqual(0, self.kube.objs[DEP]["spec"]["replicas"])
        plan = RS.cancel_plan(item)
        self.assertEqual("rollback", plan["mode"])
        message = RS.cancel_run(item, {})
        self.assertEqual("app-1-data-restore", self.cancelled[0]["name"])
        self.assertEqual(1, self.kube.objs[DEP]["spec"]["replicas"])
        self.assertIn("Nothing was changed", message)

    def test_after_the_swap_cancelling_swaps_back(self):
        _, item = self.started()
        self.run_until(item, "watching")
        message = RS.cancel_run(item, {})
        self.assertEqual("app-1-data", self.template_claim())
        self.assertEqual(1, self.kube.objs[DEP]["spec"]["replicas"])
        self.assertEqual("not-started", self.marks(NEW)[1]["homestead.io/replaced-reason"])
        self.assertEqual([], self.cancelled)
        self.assertIn("Swapped back", message)


class RegisterTests(Base):
    def test_the_storage_module_registers_the_kind(self):
        import homestead_storage_server as STORAGE_SERVER
        ops = types.SimpleNamespace(RESOLVERS={"volume-restore": lambda item: None}, RESUMABLE={}, CANCELLERS={},
                                    CLEANUPS=set(), checkpoint=lambda item: None)
        STORAGE_SERVER.register(ops)
        self.assertIn("restore-swap", ops.RESOLVERS)
        self.assertIn("restore-swap", ops.CANCELLERS)

    def test_a_second_restore_into_the_same_app_is_refused(self):
        self.ops.items.append({"kind": "restore-swap", "status": "running", "title": "Restore app-1 from a backup",
                               "resource": {"kind": "Deployment", "namespace": NS, "name": "app-1"},
                               "ref": {"namespace": NS, "app": "app-1", "old_claim": "a", "new_claim": "b"}})
        p = self.plan()
        self.assertFalse(p["ready"])
        self.assertTrue(any("already has a job" in b for b in p["blockers"]), p["blockers"])


if __name__ == "__main__":
    unittest.main()
