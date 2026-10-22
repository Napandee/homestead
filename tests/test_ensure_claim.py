import sys, tempfile, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server
import homestead_kube as K
import homestead_operations as OPS
import homestead_storage as STORAGE
from fakes.kube import Kube

CLAIMS = "/api/v1/namespaces/lab/persistentvolumeclaims"
CLAIM = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
         "metadata": {"namespace": "lab", "name": "doublecommander-appdata"}}


class EnsureClaimTests(unittest.TestCase):
    """Installing again after a failed install stopped at "persistentvolumeclaims
    doublecommander-appdata already exists": the failed one left its volume.
    On the shared fake API (tests/fakes/kube.py)."""

    def cluster(self, *objects):
        kube = self.enterContext(Kube(*objects))
        # The shared fake runs the real write guards; the storage guard takes the
        # job store's lock, which lives under its DATA_DIR.
        self.enterContext(mock.patch.object(OPS, "DATA_DIR", self.enterContext(tempfile.TemporaryDirectory())))
        self.enterContext(mock.patch.object(server, "storage_classes", lambda: []))
        # STORAGE reads the cluster through the shared client here, whatever another test bound.
        self.enterContext(mock.patch.object(STORAGE, "kget", K.kget))
        self.enterContext(mock.patch.object(STORAGE, "ksend", K.ksend))
        return kube

    def sent(self, kube):
        return [(method, path) for method, path, _, _ in kube.writes]

    def test_a_new_name_is_created(self):
        kube = self.cluster()
        self.assertEqual("", server.STORAGE_ROUTES.ensure_claim("lab", "doublecommander-appdata", 5, "longhorn-r2"))
        self.assertEqual([("POST", CLAIMS)], self.sent(kube))
        self.assertEqual("longhorn-r2", kube.object(f"{CLAIMS}/doublecommander-appdata")["spec"]["storageClassName"])

    def test_one_left_by_a_failed_install_is_taken_as_it_is(self):
        kube = self.cluster(CLAIM)
        self.assertEqual("doublecommander-appdata", server.STORAGE_ROUTES.ensure_claim("lab", "doublecommander-appdata", 5))
        self.assertEqual([], self.sent(kube))

    def test_one_another_container_uses_is_refused_by_name(self):
        kube = self.cluster(CLAIM, {
            "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"namespace": "lab", "name": "krusader"},
            "spec": {"template": {"spec": {"volumes": [
                {"name": "d", "persistentVolumeClaim": {"claimName": "doublecommander-appdata"}}]}}}})
        with self.assertRaisesRegex(ValueError, "Deployment/krusader uses it"):
            server.STORAGE_ROUTES.ensure_claim("lab", "doublecommander-appdata", 5)
        self.assertEqual([], self.sent(kube))


if __name__ == "__main__":
    unittest.main()
