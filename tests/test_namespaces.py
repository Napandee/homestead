"""Which namespaces are yours, and making and removing them."""
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_namespaces as ns


def namespace(name, annotations=None):
    return {"metadata": {"name": name, "annotations": annotations or {}}, "status": {"phase": "Active"}}


class Cluster:
    def __init__(self, names, contents=None, failing=(), missing=()):
        self.items = [namespace(n) for n in names]
        self.contents = contents or {}
        self.failing, self.missing = failing, missing
        self.sent = []

    def get(self, path):
        if path == "/api/v1/namespaces":
            return {"items": self.items}
        for kind in self.failing:
            if path.endswith("/" + kind):
                raise urllib.error.HTTPError(path, 500, "busy", {}, None)
        for kind in self.missing:
            if path.endswith("/" + kind):
                raise urllib.error.HTTPError(path, 404, "not found", {}, None)
        for (kind, name), items in self.contents.items():
            if f"/namespaces/{name}/{kind}" in path:
                return {"items": items if isinstance(items, list) else [{}] * items}
        return {"items": []}

    def send(self, method, path, body=None):
        self.sent.append((method, path, body))
        return {}


HARVESTER = ["default", "lab", "media", "kube-system", "cattle-system", "harvester-system", "longhorn-system",
             "fleet-local", "cluster-fleet-local-local-1a3d67d0a899", "p-7k2xm", "user-8fh3z", "u-b4qkz", "local",
             "cattle-impersonation-system", "harvester-public", "cdi"]


class NamespaceTests(unittest.TestCase):
    def use(self, names=HARVESTER, contents=None, **kw):
        self.cluster = Cluster(names, contents, **kw)
        ns.bind(self.cluster.get, self.cluster.send, "lab", "lab")

    def test_pickers_offer_only_places_for_apps(self):
        self.use()
        self.assertEqual(["default", "lab", "media"], ns.names())
        self.assertEqual(len(HARVESTER), len(ns.names(include_system=True)))

    def test_rancher_marks_its_own(self):
        self.assertTrue(ns.is_system(namespace("anything", {"management.cattle.io/system-namespace": "true"})))

    def test_a_namespace_is_made_and_labelled(self):
        self.use()
        ns.create("Photos")
        method, path, body = self.cluster.sent[-1]
        self.assertEqual(("POST", "/api/v1/namespaces", "photos"), (method, path, body["metadata"]["name"]))
        self.assertEqual("true", body["metadata"]["labels"]["homestead.io/managed"])
        for bad in ("kube-thing", "cattle-x", "p-abcde", "no_underscores", ""):
            with self.subTest(bad):
                with self.assertRaises(ValueError):
                    ns.create(bad)

    def test_only_an_empty_namespace_of_yours_goes_and_only_when_named(self):
        self.use(contents={("persistentvolumeclaims", "media"): 2})
        for name, reason in (("lab", "default"), ("kube-system", "platform"), ("media", "2 volumes"),
                             ("default", "Kubernetes")):
            with self.subTest(name):
                with self.assertRaisesRegex(ValueError, reason):
                    ns.delete(name, name)
        self.use(names=["lab", "old"])
        with self.assertRaisesRegex(ValueError, "type old"):
            ns.delete("old", "")
        ns.delete("old", "old")
        self.assertEqual(("DELETE", "/api/v1/namespaces/old", None), self.cluster.sent[-1])

    def test_a_failed_count_refuses_the_delete(self):
        # A short API error must not read as "no volumes here" (#599).
        self.use(names=["lab", "old"], failing=("persistentvolumeclaims",))
        with self.assertRaisesRegex(ValueError, "could not check volumes in old, so nothing was deleted"):
            ns.delete("old", "old")
        self.assertEqual([], [s for s in self.cluster.sent if s[0] == "DELETE"])

    def test_an_api_the_cluster_does_not_have_counts_as_none(self):
        self.use(names=["lab", "old"], missing=("virtualmachines", "datavolumes"))
        ns.delete("old", "old")
        self.assertEqual(("DELETE", "/api/v1/namespaces/old", None), self.cluster.sent[-1])

    def test_a_cronjob_a_daemonset_a_disk_or_a_bare_pod_is_not_empty(self):
        bare = {"metadata": {"name": "debug"}, "status": {"phase": "Running"}}
        owned = {"metadata": {"name": "web-1", "ownerReferences": [{"kind": "ReplicaSet"}]}, "status": {"phase": "Running"}}
        done = {"metadata": {"name": "once"}, "status": {"phase": "Succeeded"}}
        for kind, items, said in (("cronjobs", 1, "1 scheduled job"), ("daemonsets", 1, "1 daemon set"),
                                  ("datavolumes", 1, "1 VM disk"), ("pods", [bare, owned, done], "1 running pod")):
            with self.subTest(kind):
                self.use(names=["lab", "old"], contents={(kind, "old"): items})
                with self.assertRaisesRegex(ValueError, said):
                    ns.delete("old", "old")
        self.use(names=["lab", "old"], contents={("pods", "old"): [owned, done]})
        ns.delete("old", "old")

    def test_the_inventory_says_what_it_could_not_read_and_offers_no_delete(self):
        self.use(names=["lab", "old"], failing=("cronjobs",))
        row = next(r for r in ns.inventory()["namespaces"] if r["name"] == "old")
        self.assertEqual(["cronjobs"], row["unread"])
        self.assertFalse(row["empty"])


if __name__ == "__main__":
    unittest.main()
