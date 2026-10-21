"""Telling a node's address from a client's, and saying so (#420)."""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_client_address as CLIENTADDR
import homestead_kube as K
from fakes.kube import Kube

NODES = [
    {"apiVersion": "v1", "kind": "Node", "metadata": {"name": "node1"},
     "spec": {"podCIDR": "10.42.0.0/24", "podCIDRs": ["10.42.0.0/24"]},
     "status": {"addresses": [{"type": "InternalIP", "address": "192.0.2.11"}, {"type": "Hostname", "address": "node1"}]}},
    {"apiVersion": "v1", "kind": "Node", "metadata": {"name": "node2"},
     "spec": {"podCIDR": "10.42.1.0/24"}, "status": {"addresses": [{"type": "InternalIP", "address": "192.0.2.12"}]}},
]


class NodeAddressTests(unittest.TestCase):
    """On the shared fake API (tests/fakes/kube.py)."""
    def setUp(self):
        self.asked = 0
        kube = self.enterContext(Kube(*NODES))

        def count(method, path, body):
            self.asked += 1     # and let the fake answer
        kube.react("GET", "/api/v1/nodes", count)
        saved = CLIENTADDR.kget
        self.addCleanup(lambda: CLIENTADDR.bind(saved))
        CLIENTADDR.bind(K.kget)
        CLIENTADDR._known.update(at=0.0, networks=[], hosts=set())

    def test_a_nodes_pod_network_or_own_address_is_a_node_address(self):
        for addr in ("10.42.0.0", "10.42.1.0", "10.42.1.1", "192.0.2.11"):
            self.assertTrue(CLIENTADDR.is_node_address(addr), addr)

    def test_a_client_on_the_lan_is_not(self):
        for addr in ("192.0.2.40", "198.51.100.7", "10.43.0.1", "", "not an address"):
            self.assertFalse(CLIENTADDR.is_node_address(addr), addr)

    def test_the_nodes_are_read_at_most_every_few_minutes(self):
        CLIENTADDR.is_node_address("10.42.0.0", now=1000)
        CLIENTADDR.is_node_address("10.42.0.0", now=1000 + CLIENTADDR.TTL - 1)
        self.assertEqual(1, self.asked)
        CLIENTADDR.is_node_address("10.42.0.0", now=1000 + CLIENTADDR.TTL + 1)
        self.assertEqual(2, self.asked)

    def test_a_cluster_that_cannot_be_read_says_nothing_rather_than_guess(self):
        CLIENTADDR.bind(mock.Mock(side_effect=OSError("no cluster")))
        self.assertFalse(CLIENTADDR.is_node_address("10.42.0.0"))

    def test_the_note_explains_why_a_network_limit_cannot_match(self):
        said = CLIENTADDR.about("10.42.0.0")
        self.assertTrue(said["node_address"])
        self.assertIn("not the client's", said["note"])
        self.assertEqual({"address": "192.0.2.40", "node_address": False, "note": ""}, CLIENTADDR.about("192.0.2.40"))


class RefusalTests(unittest.TestCase):
    def test_a_key_refused_from_a_node_address_says_why(self):
        import homestead_api_keys as KEYS
        rec = {"name": "Claude", "owner": "ada", "scopes": ["read"], "networks": ["192.0.2.0/24"],
               "expires": time.time() + 3600, "hash": KEYS._digest("s" * 43)}
        data = {"api_keys": {"abcdef012345": rec}, "users": {"ada": {"role": "admin"}}}
        with mock.patch.object(KEYS.AUTH, "_load", return_value=data), \
                mock.patch.object(KEYS.AUTH, "_rate_ok", return_value=True), mock.patch.object(KEYS.AUTH, "_rate_hit"), \
                mock.patch.object(CLIENTADDR, "is_node_address", return_value=True):
            with self.assertRaises(PermissionError) as refused:
                KEYS.verify("hsk_abcdef012345_" + "s" * 43, "10.42.0.0")
        self.assertIn("cannot be used from 10.42.0.0", str(refused.exception))
        self.assertIn("not the client's", str(refused.exception))


if __name__ == "__main__":
    unittest.main()
