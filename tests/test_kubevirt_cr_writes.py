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
import homestead_macvtap as MACVTAP
import homestead_passthrough as PASSTHROUGH
import homestead_vm_write as VM_WRITE
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


class GateTests(unittest.TestCase):
    """Feature gates are one list too: a gate switched on meanwhile stays on."""
    SNAPSHOT = {"spec": {"configuration": {"developerConfiguration": {"featureGates": ["Snapshot"]}}}}

    def setUp(self):
        saved = (PASSTHROUGH.kget, PASSTHROUGH.ksend, PASSTHROUGH.hostrun, PASSTHROUGH.platform, PASSTHROUGH.DATA_DIR)
        self.addCleanup(lambda: PASSTHROUGH.bind(*saved))
        saved_macvtap = (MACVTAP.kget, MACVTAP.ksend, MACVTAP.addons, MACVTAP.platform)
        self.addCleanup(lambda: MACVTAP.bind(*saved_macvtap))

    def gates(self, kube, key="featureGates"):
        return kube.object(CR)["spec"]["configuration"]["developerConfiguration"].get(key)

    def test_ensure_gates_keeps_a_gate_switched_on_meanwhile(self):
        with Kube(kubevirt()) as kube:
            PASSTHROUGH.bind(K.kget, meanwhile(self.SNAPSHOT), None, None)
            self.assertEqual(["HostDevices"], PASSTHROUGH.ensure_gates("HostDevices"))
            self.assertEqual(["HostDevices", "Snapshot"], sorted(self.gates(kube)), "the gate switched on meanwhile was dropped")

    def test_ensure_gates_sent_through_a_review_names_the_version_it_read(self):
        sent = []
        with Kube(kubevirt()):
            PASSTHROUGH.bind(K.kget, K.send, None, None)
            PASSTHROUGH.ensure_gates("HostDevices", send=lambda *a, **o: (sent.append(a[2]), K.send(*a, **o))[1])
        self.assertTrue(sent[0]["metadata"].get("resourceVersion"))

    def test_a_reviewed_vm_save_still_stops_on_a_conflict(self):
        # A reviewed VM create or edit sends through its ResourceWriter, which
        # never replays a refused write: a gate changed meanwhile stops it.
        events = []
        with Kube(kubevirt()) as kube:
            PASSTHROUGH.bind(K.kget, K.send, None, None)
            writer = VM_WRITE.ResourceWriter(meanwhile(self.SNAPSHOT), events.append)
            with self.assertRaises(VM_WRITE.WriteFailure):
                PASSTHROUGH.ensure_gates("HostDevices", send=writer)
            self.assertEqual(["Snapshot"], self.gates(kube), "nothing of the reviewed save was written")
        self.assertEqual(["intent", "refused"], [e["phase"] for e in events], "one attempt, recorded as refused")
        self.assertEqual(409, events[-1]["http_status"])

    def test_macvtap_keeps_a_gate_switched_on_meanwhile(self):
        cr = kubevirt()
        cr["status"] = {"observedKubeVirtVersion": "v1.4.1"}   # before 1.5 it needs NetworkBindingPlugins on
        with Kube(cr) as kube:
            MACVTAP.bind(K.kget, meanwhile(self.SNAPSHOT), None, None)
            MACVTAP.register_binding()
            self.assertEqual(["NetworkBindingPlugins", "Snapshot"], sorted(self.gates(kube)), "the gate switched on meanwhile was dropped")
            self.assertIn("macvtap", kube.object(CR)["spec"]["configuration"]["network"]["binding"])

    def test_macvtap_on_a_newer_kubevirt_keeps_a_gate_switched_off_meanwhile(self):
        cr = kubevirt()
        cr["status"] = {"observedKubeVirtVersion": "v1.9.0"}
        change = {"spec": {"configuration": {"developerConfiguration": {"disabledFeatureGates": ["Snapshot"]}}}}
        with Kube(cr) as kube:
            MACVTAP.bind(K.kget, meanwhile(change), None, None)
            MACVTAP.register_binding()
            self.assertEqual([MACVTAP.GATE, "Snapshot"], sorted(self.gates(kube, "disabledFeatureGates")))


if __name__ == "__main__":
    unittest.main()
