"""Two scripts on one host don't stop each other: each run has its own helper
pod, deletes only that one, and clears away pods of runs that already ended,
never one still running (#597)."""
import sys
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_hostrun as HOSTRUN

PODS = "/api/v1/namespaces/lab/pods"


def helper(name, node, phase):
    return {"metadata": {"name": name, "namespace": "lab", "uid": f"uid-{name}",
                         "labels": {**HOSTRUN.NAMES.labels(HOSTRUN.TASK), HOSTRUN.NAMES.key("node"): node}},
            "spec": {"nodeName": node}, "status": {"phase": phase}}


class Cluster:
    def __init__(self, existing=()):
        self.pods = {f"{PODS}/{p['metadata']['name']}": p for p in existing}
        self.sent, self.ran_in = [], []

    def get(self, path):
        if path.startswith(PODS + "?"):
            return {"items": list(self.pods.values())}
        return self.pods.get(path)

    def send(self, method, path, body=None, **_):
        self.sent.append((method, path))
        if method == "POST":
            self.pods[f"{path}/{body['metadata']['name']}"] = dict(body, status={"phase": "Running"})
        elif method == "DELETE":
            self.pods.pop(path.split("?")[0], None)
        return {}

    def exec_in(self, namespace, pod, argv, **kwargs):
        self.ran_in.append(pod)
        return b"ok", ""


class RunPodTests(unittest.TestCase):
    def use(self, existing=()):
        self.cluster = Cluster(existing)
        HOSTRUN.bind(self.cluster.get, self.cluster.send, self.cluster.exec_in, "lab")
        return self.cluster

    def test_a_run_never_deletes_another_runs_helper(self):
        other = helper("homestead-host-node-2", "node-2", "Running")
        c = self.use([other])
        HOSTRUN.run("node-2", "echo ok", sleep=lambda s: None)
        deleted = [p for m, p in c.sent if m == "DELETE"]
        self.assertNotIn(f"{PODS}/homestead-host-node-2", deleted)
        self.assertIn(f"{PODS}/homestead-host-node-2", c.pods, "the other run's helper is still there")
        self.assertNotEqual("homestead-host-node-2", c.ran_in[0])

    def test_each_run_has_its_own_helper_and_removes_it(self):
        c = self.use()
        HOSTRUN.run("node-2", "echo one", sleep=lambda s: None)
        HOSTRUN.run("node-2", "echo two", sleep=lambda s: None)
        first, second = c.ran_in
        self.assertNotEqual(first, second)
        for name in (first, second):
            self.assertTrue(name.startswith("homestead-host-node-2-") and len(name) <= 63, name)
            self.assertIn(("DELETE", f"{PODS}/{name}"), c.sent)
        self.assertEqual({}, c.pods)

    def test_helpers_of_runs_that_ended_are_cleared_away(self):
        ended = helper("homestead-host-node-2-dead01", "node-2", "Failed")
        done = helper("homestead-host-node-3-dead02", "node-3", "Succeeded")
        running = helper("homestead-host-node-3-live03", "node-3", "Running")
        c = self.use([ended, done, running])
        HOSTRUN.run("node-2", "echo ok", sleep=lambda s: None)
        self.assertNotIn(f"{PODS}/homestead-host-node-2-dead01", c.pods)
        self.assertNotIn(f"{PODS}/homestead-host-node-3-dead02", c.pods)
        self.assertIn(f"{PODS}/homestead-host-node-3-live03", c.pods)

    def test_a_long_node_name_still_gives_distinct_names_that_fit(self):
        node = "a-very-long-node-name-from-a-cloud-provider-eu-west-1-instance-0001"
        names = {HOSTRUN.pod_name(node) for _ in range(20)}
        self.assertEqual(20, len(names))
        self.assertTrue(all(len(n) <= 63 and n.startswith("homestead-host-") for n in names))


if __name__ == "__main__":
    unittest.main()
