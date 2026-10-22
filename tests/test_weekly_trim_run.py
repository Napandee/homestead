"""Homestead's weekly trim (#591): every attached Longhorn volume with a
filesystem, trimmed by Homestead once a week as a job that says what it
trimmed and what it skipped."""
import calendar
import json
import os
import socket
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_longhorn as LH
import homestead_longhorn_jobs as LH_JOBS
import homestead_operations as OPS
import homestead_weekly_trim as WT

VOLS = f"{LH.API}/namespaces/{LH.LHNS}/volumes"
JOBS = f"{LH.API}/namespaces/{LH.LHNS}/recurringjobs"
MANAGED = {"homestead.io/managed": "true"}
PVS = "/api/v1/persistentvolumes"
DETACHED = ValueError("Longhorn refused the trim: Action [trimFilesystem] not available on [&{pvc-c volume map[]")


def vol(name, state, pvc=None):
    return {"metadata": {"name": name, "labels": {}},
            "status": {"state": state, "kubernetesStatus": {"pvName": name, "pvcName": pvc or f"data-{name}",
                                                            "namespace": "apps"}}}


SAT = calendar.timegm((2026, 10, 24, 4, 0, 0))


class PlanTests(unittest.TestCase):
    def test_the_slot_is_saturday_0400_utc(self):
        self.assertEqual(SAT, WT.last_slot(SAT))
        self.assertEqual(SAT, WT.last_slot(SAT + 3 * 86400))
        self.assertEqual(SAT - 7 * 86400, WT.last_slot(SAT - 1))

    def test_due_once_per_slot_and_not_when_off(self):
        self.assertTrue(WT.due({}, SAT + 60))
        self.assertFalse(WT.due({"last": {"slot": SAT}}, SAT + 60))
        self.assertFalse(WT.due({"enabled": False}, SAT + 60))
        self.assertTrue(WT.due({"last": {"slot": SAT - 7 * 86400}}, SAT + 2 * 86400), "a missed slot runs once, late")
        self.assertFalse(WT.due({"tried": SAT}, SAT + 600), "a run that could not finish is not retried every tick")

    def test_plan_trims_attached_filesystem_volumes_and_skips_the_rest(self):
        vols = [vol("pvc-c", "attached"), vol("pvc-b", "detached"), vol("pvc-a", "attached"), vol("pvc-d", "attaching")]
        got = WT.plan(vols, {"pvc-a": "Filesystem", "pvc-c": "Block"})
        self.assertEqual(["pvc-a"], got["trim"])
        self.assertEqual([("pvc-b", "data-pvc-b", "detached"), ("pvc-c", "data-pvc-c", "no filesystem (VM disk)"),
                          ("pvc-d", "data-pvc-d", "attaching")],
                         [(s["volume"], s["pvc"], s["why"]) for s in got["skipped"]])

    def test_a_volume_with_no_claim_counts_as_a_filesystem(self):
        bare = {"metadata": {"name": "pvc-e"}, "status": {"state": "attached"}}
        self.assertEqual({"trim": ["pvc-e"], "skipped": []}, WT.plan([bare], {}))

    def test_classify_reads_longhorns_answer(self):
        self.assertEqual(("trimmed", ""), WT.classify(None))
        self.assertEqual(("skipped", "detached"), WT.classify(ValueError(
            "Longhorn refused the trim: Action [trimFilesystem] not available on [&{pvc-x volume map[]")))
        self.assertEqual(("skipped", "no filesystem (VM disk)"), WT.classify(ValueError(
            "Longhorn refused the trim: unable to trim filesystem for volume pvc-x: filesystem trim is not supported "
            "on PVC demo-os in namespace demo with Block VolumeMode")))
        self.assertEqual("unconfirmed", WT.classify(urllib.error.URLError(socket.timeout("timed out")))[0])
        self.assertEqual("unconfirmed", WT.classify(TimeoutError("timed out"))[0])
        self.assertEqual(("failed", "Longhorn refused the trim: no space left"),
                         WT.classify(ValueError("Longhorn refused the trim: no space left")))
        self.assertEqual(300, len(WT.classify(ValueError("x" * 400))[1]))


class Cluster:
    """Longhorn's volumes and the PVs as the API server answers, and the
    backend's answer to each trim: an exception per volume, or None."""
    def __init__(self, volumes=(), modes=None, answers=None, unreadable=False):
        self.volumes, self.modes, self.answers = list(volumes), dict(modes or {}), dict(answers or {})
        self.unreadable, self.trimmed, self.sent = unreadable, [], []

    def get(self, path):
        if path == VOLS:
            if self.unreadable:
                raise OSError("api down")
            return {"items": self.volumes}
        if path == PVS:
            return {"items": [{"metadata": {"name": n}, "spec": {"volumeMode": m}} for n, m in self.modes.items()]}
        raise AssertionError(path)

    def send(self, method, path, body=None, **kw):
        self.sent.append((method, path))
        return {}

    def backend(self, method, path, body=None):
        self.trimmed.append((method, path, body))
        name = path.split("/")[2].split("?")[0]
        if self.answers.get(name):
            raise self.answers[name]
        return {}


class StateCase(unittest.TestCase):
    """A data folder of its own for weekly-trim.json."""
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        WT.bind(self.dir.name)
        self.checkpoints = []

    def state(self):
        try:
            with open(os.path.join(self.dir.name, WT.STATE), encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return {}

    def save(self, state):
        with open(os.path.join(self.dir.name, WT.STATE), "w", encoding="utf-8") as handle:
            json.dump(state, handle)


class JobTests(StateCase):
    def run_job(self, cluster, ref=None):
        """Step the job to its end, as the job store would; returns the last answer."""
        LH.bind(cluster.get, cluster.send, {}, "longhorn")
        item = {"id": "j1", "ref": dict(ref or {"phase": "plan", "why": "weekly", "slot": SAT})}
        with mock.patch.object(LH_JOBS, "_backend", cluster.backend):
            for _ in range(50):
                status, progress, message = WT.resolve(item, lambda i: self.checkpoints.append(json.loads(json.dumps(i))))
                if status != "running":
                    return status, progress, message
        self.fail("the job never ended")

    def test_a_run_trims_attached_skips_detached_and_says_so(self):
        lh = Cluster([vol("pvc-a", "attached"), vol("pvc-b", "detached"), vol("pvc-c", "attached")])
        self.assertEqual(("succeeded", 100, "Trimmed 2 volumes, skipped 1 (1 detached). Skipped: data-pvc-b"),
                         self.run_job(lh))
        self.assertEqual([("POST", "/volumes/pvc-a?action=trimFilesystem", {}),
                          ("POST", "/volumes/pvc-c?action=trimFilesystem", {})], lh.trimmed)
        last = self.state()["last"]
        self.assertEqual((SAT, ["pvc-a", "pvc-c"], [], []), (last["slot"], last["trimmed"], last["failed"], last["unconfirmed"]))
        self.assertEqual([{"volume": "pvc-b", "pvc": "data-pvc-b", "why": "detached"}], last["skipped"])
        self.assertTrue(self.checkpoints, "each step is checkpointed")

    def test_a_volume_that_detaches_mid_run_counts_as_skipped(self):
        lh = Cluster([vol("pvc-a", "attached"), vol("pvc-c", "attached")], answers={"pvc-c": DETACHED})
        status, _, message = self.run_job(lh)
        self.assertEqual("succeeded", status)
        self.assertEqual("Trimmed 1 volume, skipped 1 (1 detached). Skipped: data-pvc-c", message)
        self.assertEqual([{"volume": "pvc-c", "pvc": "data-pvc-c", "why": "detached"}], self.state()["last"]["skipped"])

    def test_a_failure_on_an_attached_volume_fails_the_run_and_names_it(self):
        lh = Cluster([vol("pvc-a", "attached"), vol("pvc-b", "detached"), vol("pvc-c", "attached")],
                     answers={"pvc-c": ValueError("Longhorn refused the trim: engine is not running")})
        status, _, message = self.run_job(lh)
        self.assertEqual("failed", status)
        self.assertEqual("Trimmed 1 volume, skipped 1 (1 detached). Failed: data-pvc-c: Longhorn refused the trim: "
                         "engine is not running. Skipped: data-pvc-b", message)
        failed = self.state()["last"]["failed"]
        self.assertEqual([("pvc-c", "data-pvc-c", "Longhorn refused the trim: engine is not running")],
                         [(f["volume"], f["pvc"], f["message"]) for f in failed])

    def test_block_volumes_are_skipped_not_failed(self):
        lh = Cluster([vol("pvc-a", "attached"), vol("pvc-v", "attached")], modes={"pvc-a": "Filesystem", "pvc-v": "Block"})
        self.assertEqual(("succeeded", 100, "Trimmed 1 volume, skipped 1 (1 VM disk). Skipped: data-pvc-v"), self.run_job(lh))
        self.assertEqual(["/volumes/pvc-a?action=trimFilesystem"], [path for _, path, _ in lh.trimmed])

    def test_a_timeout_is_unconfirmed_not_failed(self):
        lh = Cluster([vol("pvc-a", "attached")], answers={"pvc-a": urllib.error.URLError(socket.timeout("timed out"))})
        status, _, message = self.run_job(lh)
        self.assertEqual("succeeded", status)
        self.assertEqual("Trimmed 0 volumes, skipped 0. Not confirmed: data-pvc-a (Longhorn did not answer within 60 s)",
                         message)
        self.assertEqual(["pvc-a"], [u["volume"] for u in self.state()["last"]["unconfirmed"]])

    def test_unreadable_volumes_fail_the_run_and_keep_the_last_result(self):
        before = {"enabled": True, "last": {"slot": SAT - 7 * 86400, "trimmed": ["pvc-a"], "skipped": [],
                                            "unconfirmed": [], "failed": []}}
        self.save(before)
        lh = Cluster(unreadable=True)
        status, _, message = self.run_job(lh)
        self.assertEqual("failed", status)
        self.assertEqual("Longhorn's volumes could not be read: api down", message)
        self.assertEqual(before, self.state())
        self.assertEqual([], lh.trimmed)

    def test_a_resumed_run_carries_on_from_its_checkpoint(self):
        lh = Cluster([vol("pvc-a", "attached"), vol("pvc-c", "attached")])
        ref = {"phase": "trim", "why": "weekly", "slot": SAT, "todo": ["pvc-c"],
               "done": [{"volume": "pvc-a", "outcome": "trimmed", "why": ""}],
               "skipped": [], "pvcs": {"pvc-a": "data-pvc-a", "pvc-c": "data-pvc-c"}}
        self.assertEqual(("succeeded", 100, "Trimmed 2 volumes, skipped 0"), self.run_job(lh, ref))
        self.assertEqual(["/volumes/pvc-c?action=trimFilesystem"], [path for _, path, _ in lh.trimmed])

    def test_the_result_keeps_the_rest_of_the_state(self):
        self.save({"created": True, "groups": ["default"], "enabled": True, "retired": True})
        self.run_job(Cluster([vol("pvc-a", "attached")]))
        state = self.state()
        self.assertEqual((True, ["default"], True, True), (state["created"], state["groups"], state["enabled"], state["retired"]))

    def test_progress_names_the_volume_being_trimmed(self):
        lh = Cluster([vol("pvc-a", "attached"), vol("pvc-c", "attached")])
        LH.bind(lh.get, lh.send, {}, "longhorn")
        item = {"id": "j1", "ref": {"phase": "plan", "why": "weekly", "slot": SAT}}
        with mock.patch.object(LH_JOBS, "_backend", lh.backend):
            first = WT.resolve(item, lambda i: None)
            second = WT.resolve(item, lambda i: None)
        self.assertEqual("running", first[0])
        self.assertEqual(("running", "Trimmed data-pvc-a (1 of 2)"), (second[0], second[2]))

    def test_start_is_one_job_at_a_time_and_records_the_try(self):
        with mock.patch.object(OPS, "start", return_value={"id": "j1"}) as start, \
                mock.patch.object(WT.time, "time", return_value=SAT + 60):
            self.assertEqual({"id": "j1"}, WT.start("weekly"))
        args, kwargs = start.call_args
        self.assertEqual((WT.KIND, "Weekly trim"), args[:2])
        self.assertEqual({"phase": "plan", "why": "weekly", "slot": SAT}, args[4])
        self.assertEqual((WT.KIND,), kwargs["unless"])
        self.assertEqual(SAT, self.state()["tried"])

    def test_register_resolves_its_kind_with_the_stores_checkpoint(self):
        from types import SimpleNamespace
        ops = SimpleNamespace(RESOLVERS={}, checkpoint=object())
        WT.register(ops)
        with mock.patch.object(WT, "resolve") as resolve:
            ops.RESOLVERS[WT.KIND]({"id": "j1"})
        resolve.assert_called_once_with({"id": "j1"}, ops.checkpoint)


def recurring(name, task="filesystem-trim", cron=LH_JOBS.TRIM_CRON, groups=("default", "media"), labels=MANAGED):
    return {"metadata": {"name": name, "labels": dict(labels)},
            "spec": {"name": name, "task": task, "cron": cron, "groups": list(groups)}}


class Jobs:
    """Longhorn's RecurringJobs as the API server answers."""
    def __init__(self, jobs=(), unreadable=False):
        self.jobs, self.unreadable, self.sent = {j["metadata"]["name"]: j for j in jobs}, unreadable, []

    def get(self, path):
        if path == JOBS:
            if self.unreadable:
                raise OSError("api down")
            return {"items": list(self.jobs.values())}
        raise AssertionError(path)

    def send(self, method, path, body=None, **kw):
        self.sent.append((method, path))
        if method == "DELETE":
            self.jobs.pop(path.rsplit("/", 1)[1], None)
        return {}


class TickTests(StateCase):
    NOW = SAT + 600

    def tick(self, lh, operations=(), now=None):
        LH.bind(lh.get, lh.send, {}, "longhorn")
        with mock.patch.object(WT, "start", return_value={"id": "j1"}) as start:
            said = WT.tick(now or self.NOW, list(operations))
        return said, start

    def test_an_untouched_homestead_trim_job_is_replaced_by_the_weekly_run(self):
        self.save({"created": True, "groups": ["default", "media"]})
        lh = Jobs([recurring(LH_JOBS.TRIM_JOB, groups=("media", "default"))])
        said, start = self.tick(lh)
        self.assertIn(("DELETE", f"{JOBS}/{LH_JOBS.TRIM_JOB}"), lh.sent)
        state = self.state()
        self.assertEqual((True, True), (state["enabled"], state["retired"]))
        self.assertIn("removed", said)
        start.assert_called_once_with("weekly")

    def test_one_made_before_groups_were_recorded_is_replaced_too(self):
        self.save({"created": True})
        lh = Jobs([recurring(LH_JOBS.TRIM_JOB)])
        self.tick(lh)
        self.assertIn(("DELETE", f"{JOBS}/{LH_JOBS.TRIM_JOB}"), lh.sent)

    def test_a_trim_job_someone_changed_is_left_theirs(self):
        for changed in (recurring(LH_JOBS.TRIM_JOB, cron="0 2 * * 0"),
                        recurring(LH_JOBS.TRIM_JOB, groups=("default",)),
                        recurring(LH_JOBS.TRIM_JOB, labels={})):
            with self.subTest(changed=changed):
                self.save({"created": True, "groups": ["default", "media"]})
                lh = Jobs([changed])
                said, start = self.tick(lh)
                self.assertEqual([], lh.sent)
                state = self.state()
                self.assertEqual((False, True), (state["enabled"], state["retired"]))
                start.assert_not_called()
        self.save({"created": True, "groups": None})          # someone chose its groups before
        lh = Jobs([recurring(LH_JOBS.TRIM_JOB)])
        self.tick(lh)
        self.assertEqual(([], False), (lh.sent, self.state()["enabled"]))

    def test_one_someone_deleted_stays_off(self):
        self.save({"created": True, "groups": ["default"]})
        said, start = self.tick(Jobs([]))
        self.assertEqual((False, True), (self.state()["enabled"], self.state()["retired"]))
        start.assert_not_called()

    def test_a_new_install_runs_it(self):
        said, start = self.tick(Jobs([]))
        self.assertEqual((True, True), (self.state()["enabled"], self.state()["retired"]))
        start.assert_called_once_with("weekly")

    def test_their_own_trim_of_default_keeps_ours_off(self):
        lh = Jobs([recurring("weekly-trim", cron="0 3 * * 0", labels={})])
        said, start = self.tick(lh)
        start.assert_not_called()
        self.assertEqual([], lh.sent)
        # Their trim of other groups only does not.
        said, start = self.tick(Jobs([recurring("media-trim", groups=("media",), labels={})]))
        start.assert_called_once_with("weekly")

    def test_due_starts_one_run(self):
        self.save({"retired": True, "enabled": True, "last": {"slot": SAT - 7 * 86400}})
        said, start = self.tick(Jobs([]))
        start.assert_called_once_with("weekly")
        self.assertEqual("weekly trim started", said)
        self.save({"retired": True, "enabled": True, "last": {"slot": SAT}})
        said, start = self.tick(Jobs([]))
        start.assert_not_called()
        self.assertEqual("", said)

    def test_only_one_weekly_trim_runs_at_a_time(self):
        self.save({"retired": True, "enabled": True})
        for status in ("queued", "running"):
            said, start = self.tick(Jobs([]), [{"kind": WT.KIND, "status": status}])
            start.assert_not_called()
        said, start = self.tick(Jobs([]), [{"kind": WT.KIND, "status": "failed"}])
        start.assert_called_once_with("weekly")

    def test_nothing_is_done_when_the_jobs_cannot_be_read(self):
        self.save({"created": True, "groups": ["default"]})
        lh = Jobs(unreadable=True)
        said, start = self.tick(lh)
        self.assertEqual("weekly trim not checked: api down", said)
        self.assertEqual([], lh.sent)
        start.assert_not_called()
        self.assertEqual({"created": True, "groups": ["default"]}, self.state())


if __name__ == "__main__":
    unittest.main()
