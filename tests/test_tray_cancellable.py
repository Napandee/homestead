"""The tray offers Cancel only where cancelling is allowed: a job whose kind
refuses cancelling (OPS.CANCEL_BLOCKS, the refusals _plan_for gives) is never
shown as cancellable, whatever its kind's own view says (#595 wrap-up gap 15)."""
import itertools
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tests"))

import homestead_job_kinds as KINDS
import homestead_operations as OPS
from test_job_views import STATUSES  # the job statuses the tray's view is checked over

REFS = [{}, {"storage_protocol": 1}, {"storage_protocol": 2, "handoff_phase": "done"},
        {"phase": "prepared"}, {"phase": "accepted"}, {"phase": "evacuating"}, {"cdi_cleanup": True}]


def jobs(kinds):
    for number, (kind, status, ref) in enumerate(itertools.product(kinds, STATUSES, REFS)):
        yield {"id": f"job-{number}", "kind": kind, "title": kind, "status": status,
               "ref": dict(ref), "history": []}


class TrayFollowsRefusalsTests(unittest.TestCase):
    def test_every_refused_job_of_every_kind_is_shown_not_cancellable(self):
        import server  # noqa: F401  (fills the registries, so every kind is there)
        kinds = sorted(KINDS.kinds())
        checked = 0
        for item in jobs(kinds):
            block = OPS.CANCEL_BLOCKS.get(item["kind"])
            if not (block and block(item)):
                continue
            with self.subTest(kind=item["kind"], status=item["status"], ref=item["ref"]):
                self.assertFalse(OPS._public(item)["cancellable"])
                self.assertFalse(OPS._plan_for(item)["can"])
            checked += 1
        self.assertGreater(checked, 0)

    def test_a_refusal_registered_later_is_followed_by_the_tray(self):
        item = {"id": "x", "kind": "vm-power", "status": "running", "ref": {"phase": "prepared"}}
        self.assertTrue(OPS._public(item)["cancellable"])
        with mock.patch.dict(OPS.CANCEL_BLOCKS, {"vm-power": lambda job: "not now"}):
            self.assertFalse(OPS._plan_for(item)["can"])
            self.assertFalse(OPS._public(item)["cancellable"])

    def test_a_refusal_that_says_yes_leaves_the_job_cancellable(self):
        item = {"id": "x", "kind": "reclass", "status": "running", "ref": {}}
        self.assertEqual("", OPS.CANCEL_BLOCKS["reclass"](item))
        self.assertTrue(OPS._public(item)["cancellable"])


if __name__ == "__main__":
    unittest.main()
