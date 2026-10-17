import base64
import json
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_proxmox_vms as PVE
import homestead_unraid_vms as UVMS

# A Windows 11 VM as Proxmox's `qm config` prints it: OVMF with enrolled keys,
# a TPM, two disks on LVM-thin, the VirtIO driver ISO, a GPU and a detached disk.
WIN11 = """bios: ovmf
boot: order=scsi0;ide2;net0
cores: 4
cpu: host
efidisk0: local-lvm:vm-100-disk-0,efitype=4m,pre-enrolled-keys=1,size=4M
hostpci0: 0000:01:00,pcie=1,x-vga=1
ide2: local:iso/virtio-win-0.1.262.iso,media=cdrom,size=708140K
machine: pc-q35-8.1
memory: 8192
meta: creation-qemu=8.1.5,ctime=1712345678
name: win11-gaming
net0: virtio=BC:24:11:3A:1C:07,bridge=vmbr0,firewall=1,tag=20
numa: 0
ostype: win11
scsi0: local-lvm:vm-100-disk-1,iothread=1,size=128G,ssd=1
scsi1: local-lvm:vm-100-disk-3,size=500G
scsihw: virtio-scsi-single
smbios1: uuid=0f5c2a3e-1d4b-4c8e-9a7f-2b6d8e1f3c5a
sockets: 1
tpmstate0: local-lvm:vm-100-disk-2,size=4M,version=v2.0
unused0: local-lvm:vm-100-disk-4
vmgenid: 6a1c2d3e-0000-4000-8000-1234567890ab"""

# A Linux VM on directory storage (qcow2), with a second disk on Ceph, two
# cards and the older boot form. No name, no machine: Proxmox's defaults.
LINUX = """#Home automation
#second line of the description
bootdisk: virtio0
cores: 2
ide2: none,media=cdrom
memory: current=4096
net0: e1000=BC:24:11:AA:BB:01,bridge=vmbr0
net1: virtio=BC:24:11:AA:BB:02,bridge=vmbr1
ostype: l26
sockets: 2
virtio0: local:101/vm-101-disk-0.qcow2,size=32G
virtio1: ceph-pool:vm-101-disk-1,size=64G

[before-upgrade]
cores: 1
memory: 2048"""

GIB = 1024 ** 3
FACTS = {
    "local-lvm:vm-100-disk-1": {"path": "/dev/pve/vm-100-disk-1", "format": "raw", "virtual": 128 * GIB, "size": 128 * GIB, "used": 128 * GIB},
    "local-lvm:vm-100-disk-3": {"path": "/dev/pve/vm-100-disk-3", "format": "raw", "virtual": 500 * GIB, "size": 500 * GIB, "used": 500 * GIB},
    "local:101/vm-101-disk-0.qcow2": {"path": "/var/lib/vz/images/101/vm-101-disk-0.qcow2", "format": "qcow2",
                                      "virtual": 32 * GIB, "size": 9 * GIB, "used": 9 * GIB},
    "ceph-pool:vm-101-disk-1": {"path": "", "format": "", "virtual": 0, "size": 0, "used": 0},
}


def b64(text):
    return base64.b64encode(str(text).encode()).decode()


def listing_lines(vms, disks):
    """What LIST_SCRIPT prints, for these VMs and disks."""
    lines = [f"VM {vmid} {b64(state)} {b64(config)}" for vmid, state, config in vms]
    for volid, f in disks.items():
        info = json.dumps({"format": f["format"], "virtual-size": f["virtual"]}) if f["path"] else ""
        lines.append(f"DISK 100 {b64(volid)} {b64(f['path'])} {f['size']} {f['used']} {b64(info)}")
    return lines + ["END"]


class ConfigTests(unittest.TestCase):
    def test_config_is_read_as_keys_with_the_description_and_without_snapshots(self):
        cfg = PVE.parse_config(LINUX)
        self.assertEqual("Home automation second line of the description", cfg["description"])
        self.assertEqual("current=4096", cfg["memory"])
        self.assertEqual("2", cfg["cores"], "a snapshot's settings are not the VM's own")

    def test_a_disk_value_splits_into_its_volume_and_options(self):
        self.assertEqual(("local-lvm:vm-100-disk-1", {"iothread": "1", "size": "128G", "ssd": "1"}),
                         PVE.options("local-lvm:vm-100-disk-1,iothread=1,size=128G,ssd=1"))


class PlanTests(unittest.TestCase):
    def win(self, state="stopped"):
        return PVE.plan("100", state, PVE.parse_config(WIN11), FACTS)

    def test_a_windows_vm_maps_across_with_what_it_needs_to_boot(self):
        vm = self.win()
        self.assertEqual(("100", 100, "win11-gaming", "win11-gaming"), (vm["id"], vm["vmid"], vm["name"], vm["slug"]))
        self.assertEqual((4, "8Gi", "uefi", True, True, True), (vm["cores"], vm["memory"], vm["firmware"], vm["secure_boot"], vm["tpm"], vm["hyperv"]))
        self.assertEqual("host-passthrough", vm["cpu_model"])
        self.assertEqual("Windows 11", vm["os"])
        self.assertTrue(vm["shut_off"] and vm["ready"], vm["problem"])
        self.assertEqual(["scsi0", "scsi1"], [d["key"] for d in vm["disks"]], "the boot disk first, as the boot order says")
        self.assertEqual([0, 1], [d["index"] for d in vm["disks"]])
        self.assertEqual(("/dev/pve/vm-100-disk-1", "scsi", "raw", 128), (vm["disks"][0]["path"], vm["disks"][0]["bus"],
                                                                         vm["disks"][0]["format"], vm["disks"][0]["size_gb"]))
        self.assertEqual({"mac": "bc:24:11:3a:1c:07", "bridge": "vmbr0", "model": "virtio", "nic_model": "virtio"}, vm["nic"])

    def test_what_cannot_come_across_says_why(self):
        vm = self.win()
        whats = {d["what"]: d for d in vm["dropped"]}
        self.assertIn("virtio-win-0.1.262.iso", whats["CD-ROM"]["detail"])
        self.assertTrue(whats["GPU or PCI device"]["hardware"])
        self.assertEqual("local-lvm:vm-100-disk-4", whats["Unused disk"]["detail"])
        notes = " ".join(vm["notes"])
        self.assertIn("EFI variables", notes)
        self.assertIn("TPM", notes)
        self.assertIn("VLAN 20", notes)
        self.assertNotIn("q35, the only", notes, "already q35")

    def test_a_running_vm_is_listed_but_not_shut_off(self):
        vm = self.win("running")
        self.assertFalse(vm["shut_off"])
        self.assertTrue(vm["ready"], "ready to copy once it is shut down")

    def test_a_linux_vm_on_qcow2_with_a_ceph_disk_comes_without_that_disk(self):
        vm = PVE.plan("101", "stopped", PVE.parse_config(LINUX), FACTS)
        self.assertEqual(("vm-101", 4, "4Gi", "bios", False), (vm["name"], vm["cores"], vm["memory"], vm["firmware"], vm["hyperv"]))
        self.assertEqual("Linux", vm["os"])
        self.assertTrue(vm["ready"], vm["problem"])
        self.assertEqual(["virtio0"], [d["key"] for d in vm["disks"]])
        self.assertEqual(("qcow2", "virtio", 9 * GIB), (vm["disks"][0]["format"], vm["disks"][0]["bus"], vm["disks"][0]["size"]))
        ceph = next(d for d in vm["dropped"] if d["detail"] == "ceph-pool:vm-101-disk-1")
        self.assertIn("Ceph", ceph["reason"])
        self.assertTrue(any(d["what"] == "Second network card" for d in vm["dropped"]))
        self.assertFalse(any(d["what"] == "CD-ROM" for d in vm["dropped"]), "an empty drive is nothing to leave behind")
        self.assertEqual(("e1000", "e1000"), (vm["nic"]["model"], vm["nic"]["nic_model"]))
        self.assertIn("i440fx, Proxmox's default, becomes q35", " ".join(vm["notes"]))

    def test_a_vm_whose_boot_disk_cannot_be_read_is_not_ready_and_says_why(self):
        cfg = PVE.parse_config("scsi0: ceph-pool:vm-102-disk-0,size=32G\nmemory: 2048\nostype: l26")
        vm = PVE.plan("102", "stopped", cfg, FACTS)
        self.assertFalse(vm["ready"])
        self.assertIn("its boot disk", vm["problem"])

    def test_a_vmdk_disk_is_refused_with_how_to_convert_it(self):
        facts = {"local:103/vm-103-disk-0.vmdk": {"path": "/var/lib/vz/images/103/vm-103-disk-0.vmdk", "format": "vmdk",
                                                 "virtual": 20 * GIB, "size": GIB, "used": GIB}}
        vm = PVE.plan("103", "stopped", PVE.parse_config("sata0: local:103/vm-103-disk-0.vmdk,size=20G"), facts)
        self.assertFalse(vm["ready"])
        self.assertIn("qm disk move", vm["problem"])

    def test_a_physical_disk_passed_through_is_left_behind(self):
        cfg = PVE.parse_config("scsi0: local-lvm:vm-100-disk-1,size=128G\nsata1: /dev/disk/by-id/ata-WDC_WD40EFRX,size=4T")
        vm = PVE.plan("100", "stopped", cfg, FACTS)
        self.assertEqual(["scsi0"], [d["key"] for d in vm["disks"]])
        self.assertTrue(any("physical disk" in d["reason"] for d in vm["dropped"]))


class ListingTests(unittest.TestCase):
    def test_the_listing_is_parsed_into_mapped_vms(self):
        lines = listing_lines([("100", "stopped", WIN11), ("101", "running", LINUX)], FACTS)
        found = PVE.listing(lines)
        self.assertTrue(found["hypervisor"])
        self.assertEqual(["vm-101", "win11-gaming"], [v["name"] for v in found["vms"]])

    def test_a_host_without_qm_runs_no_vms(self):
        self.assertEqual({"hypervisor": False, "vms": []}, PVE.listing(["NOQM"]))

    def test_a_listing_cut_short_is_not_taken_as_the_whole(self):
        with self.assertRaises(ValueError):
            PVE.listing(listing_lines([("100", "stopped", WIN11)], FACTS)[:-1])

    def test_the_listing_script_reads_paths_and_never_the_disks_themselves(self):
        self.assertIn("pvesm path", PVE.LIST_SCRIPT)
        self.assertIn("qemu-img info -U", PVE.LIST_SCRIPT)
        self.assertNotIn("dd ", PVE.LIST_SCRIPT)


class ImportTests(unittest.TestCase):
    """The shared import (homestead_unraid_vms) with a Proxmox source."""
    SRC = {"name": "pve", "kind": "proxmox", "host": "192.0.2.10", "user": "root", "port": 22, "ssh_trust": {"key": "x"}}

    def setUp(self):
        self.imp = mock.Mock()
        self.imp._source.return_value = dict(self.SRC)
        self.imp._ssh_script.side_effect = lambda src, cmd: f"ssh {src['host']} {cmd!r}"
        self.imp.run_probe.return_value = listing_lines([("100", "stopped", WIN11)], FACTS)
        self.imp.SOURCE_SSH.setup.return_value = "# ssh setup\n"
        self.imp.SOURCE_SSH.command.side_effect = lambda src, cmd, compress=False: f"ssh {src['host']} {cmd!r}"
        UVMS.bind(self.imp, mock.Mock(), mock.Mock(), mock.Mock())

    def test_a_proxmox_source_lists_its_vms(self):
        found = UVMS.listing("pve")
        self.assertEqual(("proxmox", True), (found["kind"], found["virsh"]))
        self.assertEqual(["100"], [v["id"] for v in found["vms"]])
        self.assertIn("qm config", self.imp._ssh_script.call_args[0][1])

    def test_a_vm_is_picked_by_its_vmid_not_its_name(self):
        self.assertEqual("win11-gaming", UVMS._one("pve", "100")["name"])
        with self.assertRaises(ValueError):
            UVMS._one("pve", "win11-gaming")

    def test_shut_down_presses_the_power_button_by_vmid(self):
        self.imp.run_probe.side_effect = [listing_lines([("100", "running", WIN11)], FACTS), ["ok"]]
        message = UVMS.shutdown("pve", "100")["message"]
        self.assertIn("qm shutdown 100", self.imp._ssh_script.call_args[0][1])
        self.assertIn("win11-gaming", message)

    def test_the_copy_checks_qm_status_and_reads_the_volume(self):
        script = UVMS.copy_script(self.SRC, "100", "/dev/pve/vm-100-disk-1", 128 * GIB)
        self.assertIn("qm status 100", script)
        self.assertIn("'stopped'", script)
        self.assertIn("on Proxmox again", script)
        self.assertIn("cat -- /dev/pve/vm-100-disk-1", script)
        self.assertNotIn("virsh", script)


if __name__ == "__main__":
    unittest.main()


class WrongTypeTests(unittest.TestCase):
    """A Proxmox host added as Unraid: libvirt isn't there, but qm is."""
    def test_the_unraid_listing_notices_qm_and_says_proxmox(self):
        imp = mock.Mock()
        imp._source.return_value = {"name": "pve", "kind": "unraid", "host": "192.0.2.10"}
        imp._ssh_script.side_effect = lambda src, cmd: cmd
        imp.run_probe.return_value = ["NOVIRSH", "HASQM"]
        UVMS.bind(imp, mock.Mock(), mock.Mock(), mock.Mock())
        found = UVMS.listing("pve")
        self.assertEqual((False, True), (found["virsh"], found["proxmox"]))
        self.assertIn("command -v qm", imp._ssh_script.call_args[0][1])

    def test_a_server_with_neither_is_no_proxmox(self):
        self.assertFalse(UVMS.parse_listing(["NOVIRSH"])["qm"])


class VolumeSizeTests(unittest.TestCase):
    def test_a_volume_has_room_for_its_filesystem_as_well_as_the_disk(self):
        import homestead_unraid_vms as UVMS
        # The import that failed: a 1 GiB disk on a volume of exactly 1 GiB.
        self.assertEqual("1280Mi", UVMS.volume_request({"virtual": 1024 ** 3, "size_gb": 1}))
        self.assertEqual("108544Mi", UVMS.volume_request({"virtual": 100 * 1024 ** 3, "size_gb": 100}))
        self.assertEqual("1280Mi", UVMS.volume_request({"size_gb": 1}))


if __name__ == "__main__":
    unittest.main()
