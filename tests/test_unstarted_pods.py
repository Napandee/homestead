import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server
from fakes.kube import Kube

WAITING = {"state": {"waiting": {"reason": "ImagePullBackOff"}}}
RUNNING = {"state": {"running": {"startedAt": "2026-09-25T10:00:00Z"}}}
DEPLOYMENT = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"namespace": "lab", "name": "doublecommander"},
              "spec": {"selector": {"matchLabels": {"app": "doublecommander"}}}}


def pod(name, statuses, deleting="", app="doublecommander"):
    meta = {"namespace": "lab", "name": name, "labels": {"app": app}}
    if deleting:
        meta["deletionTimestamp"] = deleting
    return {"apiVersion": "v1", "kind": "Pod", "metadata": meta,
            "status": {"phase": "Pending", "containerStatuses": statuses}}


class UnstartedPodTests(unittest.TestCase):
    """Each Stop and Start of doublecommander left one more pod behind: pods
    that never started seldom finish stopping. On the shared fake API
    (tests/fakes/kube.py)."""

    def cluster(self, *pods):
        # Another app's pod that never started: the label selector keeps it out.
        return self.enterContext(Kube(DEPLOYMENT, *pods, pod("other-stuck", [WAITING], app="krusader")))

    def deleted(self, kube):
        """The pods deleted; any other write fails the test."""
        writes = [(method, path.split("?")[0]) for method, path, _, _ in kube.writes]
        self.assertTrue(all(method == "DELETE" and "/namespaces/lab/pods/" in path for method, path in writes), writes)
        return [path.rsplit("/", 1)[1] for _, path in writes]

    def test_stopping_clears_pods_that_never_started(self):
        kube = self.cluster(pod("stuck-pull", [WAITING]), pod("creating", []), pod("was-running", [RUNNING]))
        server.OVERVIEW_ROUTES.clear_unstarted_pods("lab", "doublecommander", stopping=True)
        self.assertEqual(["creating", "stuck-pull"], self.deleted(kube))
        self.assertIsNotNone(kube.object("/api/v1/namespaces/lab/pods/was-running"))

    def test_starting_clears_only_those_still_stuck_stopping(self):
        kube = self.cluster(pod("left-over", [WAITING], deleting="2026-01-01T00:00:00Z"), pod("fresh", [WAITING]))
        server.OVERVIEW_ROUTES.clear_unstarted_pods("lab", "doublecommander", stopping=False)
        self.assertEqual(["left-over"], self.deleted(kube))


if __name__ == "__main__":
    unittest.main()
