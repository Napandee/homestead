"""The account store, when the cluster is slow or the Secret is missing.
Runs on the shared fake API (tests/fakes/kube.py)."""
import base64
import json
import socket
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_auth as AUTH
import homestead_kube as K
from fakes.kube import Kube


def store_secret(store):
    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": "homestead-auth", "namespace": AUTH.NS},
            "data": {"store.json": base64.b64encode(json.dumps(store).encode()).decode()}}


class AccountStoreTests(unittest.TestCase):
    STORE = {"users": {"robin": {"salt": "s", "hash": "h", "ver": 1, "role": "admin"}}, "signing_key": "k" * 40}

    def setUp(self):
        AUTH._store_cache.update(at=0, data=None)
        self.saved_io = (AUTH.kget, AUTH.ksend, AUTH.SECRET_NAME)
        AUTH.kget, AUTH.ksend = K.kget, K.ksend
        AUTH.SECRET_NAME = lambda: "homestead-auth"
        self.path = f"/api/v1/namespaces/{AUTH.NS}/secrets/homestead-auth"
        self.slow = False

    def tearDown(self):
        AUTH.kget, AUTH.ksend, AUTH.SECRET_NAME = self.saved_io
        AUTH._store_cache.update(at=0, data=None)

    def cluster(self, *objects):
        kube = self.enterContext(Kube(*objects))

        def slow_read(method, path, body):
            # An API server that can be slow: reading the accounts Secret times out while slow.
            if self.slow:
                raise socket.timeout("timed out")
        kube.react("GET", self.path, slow_read)
        return kube

    def test_a_slow_cluster_is_never_mistaken_for_a_fresh_install(self):
        kube = self.cluster(store_secret(self.STORE))
        self.slow = True
        with self.assertRaises(AUTH.StoreUnavailable):
            AUTH.needs_setup()
        with self.assertRaises(AUTH.StoreUnavailable):
            AUTH.create_user("intruder", "a-long-password", first_only=True)
        self.assertEqual([], kube.writes, "nothing may be written over the real accounts")

    def test_the_last_good_read_stands_in_while_the_cluster_is_slow(self):
        self.cluster(store_secret(self.STORE))
        self.assertFalse(AUTH.needs_setup())
        token = AUTH.issue_token("robin")
        self.slow = True
        self.assertFalse(AUTH.needs_setup())
        self.assertEqual("robin", AUTH.verify_token(token)["user"])
        with self.assertRaises(PermissionError):
            AUTH.create_user("intruder", "a-long-password", first_only=True)

    def test_a_missing_secret_is_still_a_fresh_install(self):
        self.cluster()
        self.assertTrue(AUTH.needs_setup())


if __name__ == "__main__":
    unittest.main()
