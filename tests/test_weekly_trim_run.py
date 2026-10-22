"""Homestead's weekly trim (#591): every attached Longhorn volume with a
filesystem, trimmed by Homestead once a week as a job that says what it
trimmed and what it skipped."""
import calendar
import socket
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_weekly_trim as WT


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


if __name__ == "__main__":
    unittest.main()
