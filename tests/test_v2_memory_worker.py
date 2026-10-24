"""Cleanup never assumes that an existing hugepage pool belongs to V2."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import homestead_v2_memory_worker as W


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, relative in [('CONFIG','etc/sysctl.d/v2.conf'), ('RECEIPT','var/memory.json'), ('POOL','pool'),
                               ('BOOT','boot'), ('MACHINE','machine'), ('NUMA','numa'), ('PROC','proc')]:
            value = self.root / relative
            value.parent.mkdir(parents=True, exist_ok=True)
            p = patch.object(W, name, value); p.start(); self.addCleanup(p.stop)
        W.POOL.mkdir(); W.PROC.mkdir(); W.BOOT.write_text('boot-1'); W.MACHINE.write_text('machine-1'); W.NUMA.write_text('0')
        (W.PROC/'meminfo').write_text('MemAvailable: 16777216 kB\n')
        self.foreign = self.root/'other.conf'; self.foreign.write_text('vm.swappiness=10\n')
        for name, value in [('POLICY_DIRS', []), ('POLICY_FILES', [self.foreign])]:
            p = patch.object(W, name, value); p.start(); self.addCleanup(p.stop)
        # Fixtures deliberately live in user-writable temporary directories.
        p = patch.object(W, 'secure'); p.start(); self.addCleanup(p.stop)
        p = patch.object(W, 'atomic', side_effect=lambda path, text: path.write_text(text)); p.start(); self.addCleanup(p.stop)
        self.pool(512)

    def pool(self, total, free=None, reserved=0):
        for name, value in [('nr_hugepages',total), ('free_hugepages',total if free is None else free), ('resv_hugepages',reserved)]:
            (W.POOL/name).write_text(str(value))

    def prepare(self):
        W.reserve('node-uid','boot-1',1024,1024); self.pool(1024)
        return W.inspect('node-uid')

    def test_preexisting_pool_is_preserved_and_live_memory_is_never_shrunk(self):
        plan = self.prepare()
        self.assertEqual((512,512), (plan['baseline'],plan['owned_pages']))
        W.stage('node-uid',plan['proof'],'boot-1')
        self.assertEqual('vm.nr_hugepages=512\n', W.CONFIG.read_text())
        self.assertEqual(1024, W.counts()['nr_hugepages'])
        self.assertEqual('restart-required',W.inspect('node-uid')['phase'])
        W.BOOT.write_text('boot-2'); self.pool(512)
        self.assertEqual('complete',W.inspect('node-uid')['phase'])
        W.reserve('node-uid','boot-2',1024,1024)
        self.assertEqual(512,json.loads(W.RECEIPT.read_text())['baseline'])

    def test_legacy_pool_without_ownership_is_retained(self):
        W.CONFIG.write_text('vm.nr_hugepages=512\n')
        self.assertEqual('retained',W.inspect('node-uid')['phase'])
        W.reserve('node-uid','boot-1',1024,1024)
        self.assertEqual(512,json.loads(W.RECEIPT.read_text())['baseline'])

    def test_existing_pool_large_enough_for_v2_cannot_be_claimed(self):
        self.pool(2048); W.CONFIG.write_text('vm.nr_hugepages=2048\n')
        W.reserve('node-uid','boot-1',1024,1024)
        self.assertEqual(0,W.inspect('node-uid')['owned_pages'])

    def test_higher_legacy_boot_reservation_is_also_preserved(self):
        W.CONFIG.write_text('vm.nr_hugepages=2048\n')
        W.reserve('node-uid','boot-1',1024,1024)
        self.assertEqual(2048,json.loads(W.RECEIPT.read_text())['baseline'])

    def test_foreign_hugepage_policy_is_never_overridden_by_cleanup(self):
        self.foreign.write_text('vm.nr_hugepages=512\n')
        W.reserve('node-uid','boot-1',1024,1024);self.pool(1024)
        with self.assertRaisesRegex(ValueError,'Another host policy'):W.inspect('node-uid')
        self.assertEqual('vm.nr_hugepages=512\n',self.foreign.read_text())

    def test_modified_policy_identity_pool_or_numa_refuses_without_writes(self):
        for change in ('foreign','managed','identity','pool','numa','proof','boot'):
            with self.subTest(change=change):
                plan=self.prepare(); before=W.CONFIG.read_text()
                if change=='foreign': self.foreign.write_text('vm.nr_hugepages=4096\n')
                if change=='managed': W.CONFIG.write_text('vm.nr_hugepages=9999\n'); before=W.CONFIG.read_text()
                if change=='identity': W.MACHINE.write_text('another-machine')
                if change=='pool': self.pool(2048)
                if change=='numa': W.NUMA.write_text('0-1')
                if change=='boot': W.BOOT.write_text('boot-2')
                with self.assertRaises(ValueError): W.stage('node-uid','wrong' if change=='proof' else plan['proof'],'boot-1')
                self.assertEqual(before,W.CONFIG.read_text())
                W.RECEIPT.unlink(); W.CONFIG.unlink(); self.pool(512)
                self.foreign.write_text('vm.swappiness=10\n'); W.MACHINE.write_text('machine-1'); W.NUMA.write_text('0'); W.BOOT.write_text('boot-1')

    def test_other_consumers_and_polling_processes_block_cleanup(self):
        plan=self.prepare()
        for free, reserved in ((0,0),(512,1)):
            self.pool(1024,free,reserved)
            with self.assertRaisesRegex(ValueError,'still in use'): W.stage('node-uid',plan['proof'],'boot-1')
        self.pool(1024); process=W.PROC/'42'; process.mkdir(); (process/'comm').write_text('spdk_tgt')
        with self.assertRaisesRegex(ValueError,'polling process'): W.stage('node-uid',plan['proof'],'boot-1')
        self.assertEqual('vm.nr_hugepages=1024\n',W.CONFIG.read_text())

    def test_interrupted_staging_keeps_live_pool_and_refuses_to_guess(self):
        plan=self.prepare()
        def interrupted(path,text):
            if path==W.CONFIG: raise OSError('interrupted')
            path.write_text(text)
        with patch.object(W,'atomic',side_effect=interrupted),self.assertRaises(OSError): W.stage('node-uid',plan['proof'],'boot-1')
        self.assertEqual(1024,W.counts()['nr_hugepages'])
        with self.assertRaisesRegex(ValueError,'policy changed'): W.inspect('node-uid')

    def test_restart_is_not_completion_until_pool_matches_original(self):
        plan=self.prepare(); W.stage('node-uid',plan['proof'],'boot-1'); W.BOOT.write_text('boot-2')
        with self.assertRaisesRegex(ValueError,'different hugepage pool'): W.inspect('node-uid')


class PathSafetyTests(unittest.TestCase):
    def test_symlink_and_shared_writable_parent_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            # Even an otherwise valid receipt cannot be trusted below /tmp or
            # another shared writable ancestor; no file is changed.
            with self.assertRaises(ValueError): W.secure(Path(folder)/'receipt')
