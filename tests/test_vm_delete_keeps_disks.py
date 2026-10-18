"""Deleting a VM and keeping its disks keeps them: a disk is let go of before
the VM goes, and if that can't be done or checked, nothing is deleted (#600)."""
import copy
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_vms as VMS

VM = {"metadata": {"name": "web", "namespace": "lab", "uid": "u1", "resourceVersion": "10"},
      "spec": {"runStrategy": "Halted",
               "dataVolumeTemplates": [{"metadata": {"name": "web-disk"},
                                        "spec": {"source": {"blank": {}},
                                                 "pvc": {"resources": {"requests": {"storage": "20Gi"}}}}}],
               "template": {"spec": {
                   "domain": {"cpu": {"cores": 2}, "memory": {"guest": "2Gi"},
                              "devices": {"disks": [{"name": "root", "disk": {"bus": "virtio"}, "bootOrder": 1}]}},
                   "volumes": [{"name": "root", "dataVolume": {"name": "web-disk"}}]}}},
      "status": {"printableStatus": "Stopped"}}
OWNED = [{"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine", "name": "web", "uid": "u1"}]
DV = "/apis/cdi.kubevirt.io/v1beta1/namespaces/lab/datavolumes/web-disk"
PVC = "/api/v1/namespaces/lab/persistentvolumeclaims/web-disk"


class Cluster:
    """A stopped VM whose one disk finished importing; the VM owns both the
    DataVolume and its claim, as dataVolumeTemplates leave them."""
    def __init__(self, failing=(), missing=(), sticky=False):
        self.owners = {DV: copy.deepcopy(OWNED), PVC: copy.deepcopy(OWNED)}
        self.failing, self.missing, self.sticky = failing, missing, sticky
        self.sent = []
        VMS.bind(self.get, self.send, lambda *a, **k: [])
        VMS.platform = lambda: {"harvester": False, "cdi": True}

    def get(self, path):
        if path in self.failing:
            raise urllib.error.HTTPError(path, 500, "busy", None, None)
        if path in self.missing:
            raise urllib.error.HTTPError(path, 404, "missing", None, None)
        if path.endswith("/virtualmachines/web"):
            return copy.deepcopy(VM)
        if path in self.owners:
            return {"metadata": {"name": "web-disk", "ownerReferences": copy.deepcopy(self.owners[path])},
                    "status": {"phase": "Succeeded"}}
        if path.endswith("/namespaces/lab/datavolumes"):
            return {"items": [{"metadata": {"name": "web-disk", "namespace": "lab"}, "status": {"phase": "Succeeded"}}]}
        if path.endswith("/persistentvolumeclaims"):
            return {"items": [{"metadata": {"name": "web-disk"}}]}
        raise urllib.error.HTTPError(path, 404, "missing", None, None)

    def send(self, method, path, body=None, **kw):
        self.sent.append((method, path, body))
        if method == "PATCH" and path in self.owners and not self.sticky:
            self.owners[path] = (body.get("metadata") or {}).get("ownerReferences") or []
        return body or {}

    def deleted_vm(self):
        return any(m == "DELETE" and p.endswith("/virtualmachines/web") for m, p, _ in self.sent)


class KeepDisksTests(unittest.TestCase):
    def test_kept_disks_are_let_go_before_the_vm_is_deleted(self):
        c = Cluster()
        VMS.delete("lab", "web")
        self.assertEqual({DV: [], PVC: []}, c.owners)
        self.assertTrue(c.deleted_vm())

    def test_a_failed_read_of_a_kept_disk_stops_the_delete(self):
        for path in (DV, PVC):
            with self.subTest(path):
                c = Cluster(failing=(path,))
                with self.assertRaisesRegex(ValueError, "web-disk could not be checked, so nothing was deleted"):
                    VMS.delete("lab", "web")
                self.assertFalse(c.deleted_vm())

    def test_a_disk_without_a_datavolume_still_has_its_claim_let_go(self):
        # A DataVolume CDI already cleaned up is not found; the claim is what keeps the data.
        c = Cluster(missing=(DV,))
        VMS.delete("lab", "web")
        self.assertEqual([], c.owners[PVC])
        self.assertTrue(c.deleted_vm())

    def test_a_disk_that_still_names_the_vm_stops_the_delete(self):
        c = Cluster(sticky=True)
        with self.assertRaisesRegex(ValueError, "web-disk still belongs to web, so nothing was deleted"):
            VMS.delete("lab", "web")
        self.assertFalse(c.deleted_vm())


if __name__ == "__main__":
    unittest.main()
