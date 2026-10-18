import copy
import sys
import unittest
import urllib.error
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import homestead_imports as imports


JOB = {"name": "nightly", "schedule": "0 3 * * *", "image": "alpine:3.24", "command": "echo hi"}


class ScheduleJobTests(unittest.TestCase):
    def setUp(self):
        self.existing = {}
        self.sent = []

        def get(path):
            if "/cronjobs" in path:
                if path.endswith("/cronjobs"):
                    return {"items": list(self.existing.values())}
                name = path.rsplit("/", 1)[1]
                if name in self.existing:
                    return copy.deepcopy(self.existing[name])
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)

        def send(method, path, body=None, **kwargs):
            self.sent.append((method, path, copy.deepcopy(body)))
            return body or {}

        self.cache = {"cron": (0, [])}
        imports.bind(get, send, None, None, "lab", self.cache)

    def cronjob(self, name, time_zone=None, labels=None):
        spec = {"schedule": "0 3 * * *", "jobTemplate": {"spec": {"template": {"spec": {
            "containers": [{"name": "task", "image": "alpine:3.24", "command": ["sh", "-c", "echo hi"]}]}}}}}
        if time_zone:
            spec["timeZone"] = time_zone
        labels = {"homestead.io/managed": "true"} if labels is None else labels
        return {"metadata": {"name": name, "resourceVersion": "7", "generation": 3, "labels": labels},
                "spec": spec, "status": {}}

    def backup_job(self, labels=None):
        """A CronJob with the parts a saved schedule must not lose."""
        job = self.cronjob("backup", "Etc/UTC", labels)
        job["metadata"]["annotations"] = {"example.com/owner": "ops"}
        pod = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        pod["serviceAccountName"] = "backup"
        pod["volumes"] = [{"name": "data", "persistentVolumeClaim": {"claimName": "app-data"}}]
        pod["containers"][0]["env"] = [{"name": "TARGET", "valueFrom": {"secretKeyRef": {"name": "s3", "key": "url"}}}]
        pod["containers"][0]["volumeMounts"] = [{"name": "data", "mountPath": "/data"}]
        return job

    def test_a_new_schedule_runs_on_utc(self):
        imports.save_job(dict(JOB))
        method, _, body = self.sent[-1]
        self.assertEqual("POST", method)
        self.assertEqual("Etc/UTC", body["spec"]["timeZone"])

    def test_editing_a_schedule_keeps_the_zone_it_already_has(self):
        self.existing["nightly"] = self.cronjob("nightly", "Europe/Berlin")
        imports.save_job(dict(JOB, schedule="0 4 * * *"))
        method, _, body = self.sent[-1]
        self.assertEqual("PUT", method)
        self.assertEqual("Europe/Berlin", body["spec"]["timeZone"])
        self.assertEqual("0 4 * * *", body["spec"]["schedule"])

    def test_editing_a_schedule_with_no_zone_pins_it_to_utc(self):
        self.existing["nightly"] = self.cronjob("nightly")
        imports.save_job(dict(JOB))
        self.assertEqual("Etc/UTC", self.sent[-1][2]["spec"]["timeZone"])

    def test_the_list_says_which_zone_each_schedule_uses(self):
        self.existing["nightly"] = self.cronjob("nightly", "Etc/UTC")
        self.existing["old"] = self.cronjob("old")
        jobs = {j["name"]: j for j in imports.list_jobs()}
        self.assertEqual("Etc/UTC", jobs["nightly"]["time_zone"])
        self.assertEqual("", jobs["old"]["time_zone"])

    def test_saving_a_schedule_keeps_the_rest_of_the_cronjob(self):
        self.existing["backup"] = self.backup_job()
        imports.save_job({"name": "backup", "schedule": "0 4 * * *", "image": "alpine:3.25",
                          "command": "tar c /data", "suspend": True, "generation": 3})
        method, path, body = self.sent[-1]
        self.assertEqual(("PUT", "/apis/batch/v1/namespaces/lab/cronjobs/backup"), (method, path))
        self.assertEqual("7", body["metadata"]["resourceVersion"], "Kubernetes refuses it if the job changed since")
        self.assertEqual({"example.com/owner": "ops"}, body["metadata"]["annotations"])
        self.assertEqual("true", body["metadata"]["labels"]["homestead.io/managed"])
        self.assertEqual("0 4 * * *", body["spec"]["schedule"])
        self.assertTrue(body["spec"]["suspend"])
        pod = body["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        self.assertEqual("backup", pod["serviceAccountName"])
        self.assertEqual("app-data", pod["volumes"][0]["persistentVolumeClaim"]["claimName"])
        container = pod["containers"][0]
        self.assertEqual("alpine:3.25", container["image"])
        self.assertEqual(["sh", "-c", "tar c /data"], container["command"])
        self.assertEqual("TARGET", container["env"][0]["name"])
        self.assertEqual("/data", container["volumeMounts"][0]["mountPath"])

    def test_a_cronjob_homestead_did_not_make_is_not_changed(self):
        self.existing["backup"] = self.backup_job(labels={"app.kubernetes.io/managed-by": "Helm"})
        with self.assertRaisesRegex(ValueError, "Homestead did not make"):
            imports.save_job({"name": "backup", "schedule": "0 4 * * *", "command": "true", "generation": 3})
        with self.assertRaisesRegex(ValueError, "Homestead did not make"):
            imports.save_job(dict(JOB, name="backup"))
        with self.assertRaisesRegex(ValueError, "Homestead did not make"):
            imports.del_job("backup")
        self.assertEqual([], self.sent)

    def test_a_schedule_changed_since_the_page_read_it_is_refused(self):
        self.existing["nightly"] = self.cronjob("nightly")
        self.existing["nightly"]["metadata"]["generation"] = 4
        with self.assertRaisesRegex(ValueError, "changed since"):
            imports.save_job(dict(JOB, generation=3))
        self.assertEqual([], self.sent)

    def test_a_run_starting_since_the_page_read_it_does_not_block_a_save(self):
        # The CronJob controller writes status on every run: a new
        # resourceVersion, the same generation.
        self.existing["nightly"] = self.cronjob("nightly")
        self.existing["nightly"]["metadata"]["resourceVersion"] = "9"
        self.existing["nightly"]["status"] = {"active": [{"name": "nightly-1"}], "lastScheduleTime": "2026-10-18T03:00:00Z"}
        imports.save_job(dict(JOB, generation=3))
        method, _, body = self.sent[-1]
        self.assertEqual("PUT", method)
        self.assertEqual("9", body["metadata"]["resourceVersion"], "the version just read")

    def test_saving_and_deleting_clear_the_cached_list(self):
        self.existing["nightly"] = self.cronjob("nightly")
        imports.save_job(dict(JOB, generation=3))
        self.assertNotIn("cron", self.cache)
        self.cache["cron"] = (0, [])
        imports.del_job("nightly")
        self.assertNotIn("cron", self.cache)

    def test_a_schedule_made_before_the_rename_is_still_homesteads(self):
        # save_job labelled them harvui.io/managed until 2026-09-22, and the
        # start-up adoption of old keys does not cover CronJobs.
        self.existing["nightly"] = self.cronjob("nightly", labels={"harvui.io/managed": "true"})
        self.assertTrue(imports.list_jobs()[0]["managed"])
        imports.save_job(dict(JOB, generation=3))
        method, _, body = self.sent[-1]
        self.assertEqual("PUT", method)
        self.assertEqual({"homestead.io/managed": "true"}, body["metadata"]["labels"])
        imports.del_job("nightly")
        self.assertEqual("DELETE", self.sent[-1][0])

    def test_a_change_between_reading_and_saving_is_refused(self):
        self.existing["nightly"] = self.cronjob("nightly")

        def conflict(method, path, body=None, **kwargs):
            raise urllib.error.HTTPError(path, 409, "Conflict", {}, None)
        imports.ksend = conflict
        with self.assertRaisesRegex(ValueError, "changed since"):
            imports.save_job(dict(JOB, generation=3))

    def test_a_new_schedule_does_not_take_over_an_existing_name(self):
        self.existing["nightly"] = self.cronjob("nightly")
        with self.assertRaisesRegex(ValueError, "already exists"):
            imports.save_job(dict(JOB, generation=""))
        self.assertEqual([], self.sent)

    def test_the_list_says_which_schedules_homestead_manages(self):
        self.existing["nightly"] = self.cronjob("nightly")
        self.existing["backup"] = self.backup_job(labels={"app.kubernetes.io/managed-by": "Helm"})
        self.existing["other"] = self.cronjob("other", labels={})
        jobs = {j["name"]: j for j in imports.list_jobs()}
        self.assertEqual((True, "", 3), (jobs["nightly"]["managed"], jobs["nightly"]["managed_by"],
                                         jobs["nightly"]["generation"]))
        self.assertEqual((False, "Helm"), (jobs["backup"]["managed"], jobs["backup"]["managed_by"]))
        self.assertEqual((False, ""), (jobs["other"]["managed"], jobs["other"]["managed_by"]))

    def test_delete_and_run_check_the_name(self):
        for bad in ("../secrets/x", "a/b", "Nightly", "a" * 53, "-x", "x."):
            with self.subTest(name=bad):
                with self.assertRaisesRegex(ValueError, "lowercase"):
                    imports.del_job(bad)
                with self.assertRaisesRegex(ValueError, "lowercase"):
                    imports.run_job_now(bad)
        self.assertEqual([], self.sent)

    def test_run_now_takes_any_name_kubernetes_allows_for_a_cronjob(self):
        for name in ("paperless-ngx-document-consumer-cleanup-job", "backup.v2", "a" * 52):
            with self.subTest(name=name):
                self.existing[name] = self.backup_job(labels={})
                imports.run_job_now(name)
                self.assertEqual("POST", self.sent[-1][0])

    def test_deleting_a_homestead_schedule_still_works(self):
        self.existing["nightly"] = self.cronjob("nightly")
        imports.del_job("nightly")
        self.assertEqual("DELETE", self.sent[-1][0])

    def test_run_now_works_on_any_schedule_as_it_changes_none(self):
        self.existing["backup"] = self.backup_job(labels={})
        imports.run_job_now("backup")
        method, path, body = self.sent[-1]
        self.assertEqual(("POST", "/apis/batch/v1/namespaces/lab/jobs"), (method, path))
        self.assertEqual("backup", body["spec"]["template"]["spec"]["serviceAccountName"])


if __name__ == "__main__":
    unittest.main()
