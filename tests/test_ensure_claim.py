import sys, unittest, urllib.error
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server
import homestead_storage as STORAGE

CLAIM = "/api/v1/namespaces/lab/persistentvolumeclaims/doublecommander-appdata"


class EnsureClaimTests(unittest.TestCase):
    """Installing again after a failed install stopped at "persistentvolumeclaims
    doublecommander-appdata already exists": the failed one left its volume."""

    def setUp(self):
        # STORAGE reads the cluster through server.py's client in these tests.
        for name in ("kget", "ksend"):
            self.addCleanup(setattr, STORAGE, name, getattr(STORAGE, name))
            setattr(STORAGE, name, lambda *a, _name=name, **k: getattr(server.KUBE_HELPERS, _name)(*a, **k))
        self.saved = server.KUBE_HELPERS.kget, server.KUBE_HELPERS.ksend, server.storage_classes
        self.claim, self.deployments, self.sent = None, [], []

        def kget(path):
            if path == CLAIM:
                if self.claim is None:
                    raise urllib.error.HTTPError(path, 404, "missing", {}, None)
                return self.claim
            if path == "/apis/apps/v1/deployments":
                return {"items": self.deployments}
            return {"items": []}
        server.KUBE_HELPERS.kget = kget
        server.KUBE_HELPERS.ksend = lambda method, path, body=None, **kw: self.sent.append((method, path))
        server.storage_classes = lambda: []

    def tearDown(self):
        server.KUBE_HELPERS.kget, server.KUBE_HELPERS.ksend, server.storage_classes = self.saved

    def test_a_new_name_is_created(self):
        self.assertEqual("", server.STORAGE_ROUTES.ensure_claim("lab", "doublecommander-appdata", 5, "longhorn-r2"))
        self.assertEqual([("POST", "/api/v1/namespaces/lab/persistentvolumeclaims")], self.sent)

    def test_one_left_by_a_failed_install_is_taken_as_it_is(self):
        self.claim = {"metadata": {"name": "doublecommander-appdata"}}
        self.assertEqual("doublecommander-appdata", server.STORAGE_ROUTES.ensure_claim("lab", "doublecommander-appdata", 5))
        self.assertEqual([], self.sent)

    def test_one_another_container_uses_is_refused_by_name(self):
        self.claim = {"metadata": {"name": "doublecommander-appdata"}}
        self.deployments = [{"metadata": {"namespace": "lab", "name": "krusader"},
                             "spec": {"template": {"spec": {"volumes": [
                                 {"name": "d", "persistentVolumeClaim": {"claimName": "doublecommander-appdata"}}]}}}}]
        with self.assertRaisesRegex(ValueError, "Deployment/krusader uses it"):
            server.STORAGE_ROUTES.ensure_claim("lab", "doublecommander-appdata", 5)
        self.assertEqual([], self.sent)


if __name__ == "__main__":
    unittest.main()
