"""Only the reviewed, dedicated filesystem region may become a V2 disk."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from test_disk_v2 import Cluster, HOST_FACTS
import homestead_disk_v2 as V
import homestead_disk_region as R


def region(kind='lvm'):
    device = '/dev/dm-1' if kind == 'lvm' else '/dev/nvme0n1p4'
    stable = '/dev/disk/by-id/dm-uuid-LVM-test-region' if kind == 'lvm' else '/dev/disk/by-partuuid/test-region'
    layout = {'blockdevices': [{'kname':'/dev/nvme0n1','type':'disk','size':2000*1024**3,'children':[
        {'kname':'/dev/nvme0n1p1','type':'part','size':1024**3,'start':2048,'mountpoint':'/boot'},
        {'kname':'/dev/nvme0n1p3','type':'part','size':1998*1024**3,'start':2099200,'children':[
            {'kname':'/dev/dm-0','type':'lvm','size':100*1024**3,'mountpoint':'/'},
            {'kname':device,'type':kind,'size':1000*1024**3,'mountpoint':'/var/lib/longhorn'}]}]}]}
    return (HOST_FACTS.replace('DEVICE /dev/sdb', 'DEVICE ' + device)
            .replace('KIND disk', 'KIND ' + kind).replace('END', '') +
            '\nREGION ' + 'a' * 64 + '\nREGION_PATH ' + stable +
            '\nPARENTS /dev/nvme0n1p3 /dev/nvme0n1\nLAYOUT '+json.dumps(layout)+'\nEND')


class RegionConversionTests(unittest.TestCase):
    def setUp(self):
        for name in ('kget', 'ksend', 'platform', 'ops', 'NS', 'read_log'):
            saved = patch.object(V, name, getattr(V, name)); saved.start(); self.addCleanup(saved.stop)
        self.cluster = Cluster()
        self.host = patch.object(V.HOST, 'run', side_effect=self.cluster.host)
        self.host_mock = self.host.start(); self.addCleanup(self.host.stop)

    def test_partition_and_lv_reviews_identify_exact_region_and_protect_parent(self):
        for kind in ('part', 'lvm'):
            self.cluster.facts = region(kind)
            plan = self.cluster.review()
            self.assertEqual(plan['region_kind'], kind)
            self.assertIn('/dev/nvme0n1', plan['protected_parents'])
        self.assertEqual(self.cluster.writes, [])

    def test_separate_default_longhorn_mount_is_allowed_but_system_mount_is_not(self):
        self.cluster.facts = region()
        facts = V._host_facts('/var/lib/longhorn/', 'node-1')
        self.assertEqual(facts['mountpoint'], '/var/lib/longhorn')
        for path in ('/', '/var', '/boot', '/var/lib/rancher', '/var/lib/longhorn/other'):
            with self.assertRaises(ValueError): V._host_facts(path, 'node-1')

    def test_geometry_change_invalidates_evacuate_and_erase_reviews(self):
        self.cluster.facts = region()
        plan = self.cluster.review()
        self.cluster.facts = region().replace('a' * 64, 'b' * 64)
        with self.assertRaises(ValueError): self.cluster.start(plan)
        self.cluster.facts = region()
        self.cluster.awaiting()
        self.cluster.facts = region().replace('a' * 64, 'b' * 64)
        with self.assertRaises(ValueError): V.prepare_review(self.cluster.ops.items[0]['id'])

    def test_region_uses_aio_and_rechecks_identity_at_each_destructive_boundary(self):
        self.cluster.facts = region()
        self.cluster.awaiting(); self.cluster.prepare()
        ref = self.cluster.ops.items[0]['ref']
        script = V._host_script(ref)
        self.assertEqual(V._new_disk(ref)['diskDriver'], 'aio')
        self.assertEqual(script.count('Region identity or geometry changed'), 2)
        self.assertLess(script.rindex('Region identity or geometry changed'), script.index('wipefs -a'))
        for forbidden in ('lvremove ', 'vgremove ', 'pvremove ', 'parted ', 'sgdisk ', 'wipefs -a "$PD"'):
            self.assertNotIn(forbidden, script)
        V._verify_blank(ref)
        self.assertIn('Region identity or geometry changed', self.host_mock.mock_calls[-1].args[1])

    def test_parent_cannot_be_mutated_while_saved_region_conversion_exists(self):
        self.cluster.facts = region(); self.cluster.start()
        with self.assertRaises(ValueError): V.mutation_guard('node-1', device='/dev/nvme0n1')
        V.mutation_guard('node-1', device='/dev/sdc')

    def test_missing_or_inconsistent_region_identity_is_refused(self):
        base = {'KIND': 'lvm', 'REGION': 'a' * 64, 'REGION_PATH': '/dev/disk/by-id/dm-uuid-LVM-test', 'PARENTS': '/dev/nvme0n1'}
        for key, value in [('REGION', ''), ('REGION_PATH', '/dev/nvme0n1'), ('PARENTS', ''), ('PARENTS', '/dev/../root'), ('KIND', 'crypt')]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError): R.facts({**base, key: value})


@unittest.skipUnless(sys.platform == 'linux', 'shell fixture uses Linux tools')
class RegionShellTests(unittest.TestCase):
    """Execute the actual checks with synthetic block topology, never devices."""
    fixture = r'''
set -eu
D=/dev/dm-1
fail() { echo "ERROR $*"; exit 1; }
lsblk() {
  case "$*" in
    '-dnro TYPE '*) echo "${KIND:-lvm}";;
    '-nrpo NAME '*) echo "$D";;
    '-snrpo KNAME,TYPE '*)
      if [ "${KIND:-lvm}" = part ]; then echo "$D part"; else echo "$D lvm"; echo '/dev/sda3 part'; fi
      echo '/dev/sda disk'; [ "${MULTI:-no}" = no ] || echo '/dev/sdb disk';;
    '-dnro TRAN '*) echo "${TRAN:-nvme}";;
    '-dnro SERIAL '*) echo fixture-parent;;
    '-dnro WWN '*) echo fixture-wwn;;
    '-dnro PARTUUID '*) echo fixture-part;;
    '-snrpb '*) echo "${GEOMETRY:-geometry-one}";;
    '-nrpb '*) echo "${SIBLINGS:-siblings-one}";;
    *) exit 2;;
  esac
}
ls() { [ "${HOLDER:-no}" = no ] || echo child; }
readlink() {
  case "$2" in
    /dev/disk/*) [ "${TEST_STABLE:-yes}" = yes ] && echo "$D" || echo /dev/other;;
    /sys/*) echo /sys/devices/pci0000/fixture;;
    *) echo "$2";;
  esac
}
lvs() { echo "fixture-lv|fixture-vg|${SEGMENT:-linear}|${ATTR:--wi-ao----}|/dev/sda3(0)"; }
cat() { echo LVM-fixture; }
findmnt() { [ "${SYSTEM:-no}" = no ] && echo /dev/os || echo "$D"; }
'''

    def run_check(self, **options):
        script = self.fixture + R.inspection() + '\nprintf "%s\\n" "$REGION"\n'
        return subprocess.run(['sh', '-c', script], env=dict(os.environ, **options), capture_output=True, text=True, timeout=5)

    def test_partition_and_linear_lv_have_stable_distinct_identities(self):
        lv = self.run_check(); part = self.run_check(KIND='part')
        self.assertEqual(lv.returncode, 0, lv.stdout + lv.stderr)
        self.assertEqual(part.returncode, 0, part.stdout + part.stderr)
        self.assertNotEqual(lv.stdout, part.stdout)
        self.assertNotEqual(lv.stdout, self.run_check(GEOMETRY='changed-extents').stdout)

    def test_shared_system_thin_multi_parent_holder_and_network_disks_are_rejected(self):
        for options in ({'SYSTEM':'yes'}, {'SEGMENT':'thin'}, {'ATTR':'owi-ao----'}, {'MULTI':'yes'}, {'HOLDER':'yes'}, {'TEST_STABLE':'no'}, {'TRAN':'iscsi'}):
            with self.subTest(options=options):
                result = self.run_check(**options)
                self.assertNotEqual(result.returncode, 0, result.stdout)


if __name__ == '__main__': unittest.main()
