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

        imports.bind(get, send, None, None, "lab", {})

    def cronjob(self, name, time_zone=None):
        spec = {"schedule": "0 3 * * *", "jobTemplate": {"spec": {"template": {"spec": {
            "containers": [{"name": "task", "image": "alpine:3.24", "command": ["sh", "-c", "echo hi"]}]}}}}}
        if time_zone:
            spec["timeZone"] = time_zone
        return {"metadata": {"name": name}, "spec": spec, "status": {}}

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


if __name__ == "__main__":
    unittest.main()
