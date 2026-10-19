"""KubeVirt's settings are changed on what is stored when they are saved, so
two changes made at once both stay (#621)."""
import io
import os
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
import homestead_kubevirt_cr as KUBEVIRT_CR
import homestead_passthrough as PASSTHROUGH
from fakes.kube import Kube

CR = "/apis/kubevirt.io/v1/namespaces/kubevirt/kubevirts/kubevirt"


def kubevirt(devices=None, gates=None):
    return {"apiVersion": "kubevirt.io/v1", "kind": "KubeVirt",
            "metadata": {"name": "kubevirt", "namespace": "kubevirt"},
            "spec": {"configuration": {"permittedHostDevices": devices or {},
                                       "developerConfiguration": {"featureGates": gates or []}}}}


def meanwhile(change):
    """A send that lets another writer change the CR just before this one's first write to it."""
    done = []

    def send(method, path, body=None, **options):
        if path == CR and not done:
            done.append(1)
            K.send("PATCH", CR, change, ctype="application/merge-patch+json")
        return K.send(method, path, body, **options)
    return send


class PermitTests(unittest.TestCase):
    def setUp(self):
        saved = (PASSTHROUGH.kget, PASSTHROUGH.ksend, PASSTHROUGH.hostrun, PASSTHROUGH.platform, PASSTHROUGH.DATA_DIR)
        self.addCleanup(lambda: PASSTHROUGH.bind(*saved))

    def devices(self, kube):
        return kube.object(CR)["spec"]["configuration"]["permittedHostDevices"]

    def test_a_device_permitted_meanwhile_is_kept(self):
        nic = {"pciVendorSelector": "8086:1533", "resourceName": PASSTHROUGH.pci_resource("8086", "1533")}
        with Kube(kubevirt(gates=["HostDevices"])) as kube:
            # Another admin permits a network card just before this one saves a GPU.
            PASSTHROUGH.bind(K.kget, meanwhile({"spec": {"configuration": {"permittedHostDevices": {"pciHostDevices": [nic]}}}}), None, None)
            PASSTHROUGH._permit(pci=("10de", "1b80"))
            devices = self.devices(kube)
        self.assertEqual(sorted([nic["resourceName"], PASSTHROUGH.pci_resource("10de", "1b80")]),
                         sorted(r["resourceName"] for r in devices["pciHostDevices"]), "the device permitted meanwhile was dropped")

    def test_a_device_removed_meanwhile_is_not_brought_back(self):
        gpu = {"pciVendorSelector": "10DE:1B80", "resourceName": PASSTHROUGH.pci_resource("10de", "1b80")}
        with Kube(kubevirt({"pciHostDevices": [gpu]}, gates=["HostDevices"])) as kube:
            PASSTHROUGH.bind(K.kget, meanwhile({"spec": {"configuration": {"permittedHostDevices": {"pciHostDevices": []}}}}), None, None)
            PASSTHROUGH._permit(usb=("1234", "5678"))
            devices = self.devices(kube)
        self.assertEqual([], devices.get("pciHostDevices"), "a device removed meanwhile came back")
        self.assertEqual([PASSTHROUGH.usb_resource("1234", "5678")], [r["resourceName"] for r in devices["usb"]])


class UpdateTests(unittest.TestCase):
    """update() tries again only on a conflict, and not for ever."""
    KV = {"metadata": {"name": "kubevirt", "namespace": "kubevirt", "uid": "u1", "resourceVersion": "7"}, "spec": {}}

    def failing(self, code):
        sent = []

        def write(method, path, body, **options):
            sent.append(body)
            raise urllib.error.HTTPError(path, code, "refused", {}, io.BytesIO(b"{}"))
        return sent, write

    def test_four_conflicts_in_a_row_stop_with_nothing_saved(self):
        sent, write = self.failing(409)
        with self.assertRaisesRegex(RuntimeError, "kept changing"):
            KUBEVIRT_CR.update(lambda kv: {"spec": {"x": 1}}, current=lambda: self.KV, write=write)
        self.assertEqual(4, len(sent))
        self.assertEqual({"uid": "u1", "resourceVersion": "7"}, sent[0]["metadata"])

    def test_any_other_refusal_is_raised_at_once(self):
        sent, write = self.failing(500)
        with self.assertRaises(urllib.error.HTTPError):
            KUBEVIRT_CR.update(lambda kv: {"spec": {"x": 1}}, current=lambda: self.KV, write=write)
        self.assertEqual(1, len(sent))


if __name__ == "__main__":
    unittest.main()
