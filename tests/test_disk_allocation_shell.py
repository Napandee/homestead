"""Run generated growth scripts against files and fake device tools only."""
import hashlib
import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_disk_allocation import A, inventory


@unittest.skipUnless(sys.platform == 'linux', 'isolated POSIX host fixtures')
class AllocationShellTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        for path in ('bin','dev/disk/by-id','dev/mapper','var/lib/longhorn','proc/sys/kernel/random','sys/class/block/dm-0/dm','sys/class/block/dm-1/dm'):
            (self.root/path).mkdir(parents=True)
        (self.root/'proc/sys/kernel/random/boot_id').write_text('boot-test')
        (self.root/'sys/class/block/nvme0n1').mkdir(parents=True)
        (self.root/'sys/devices/pci0000').mkdir(parents=True)
        (self.root/'sys/class/block/nvme0n1/device').symlink_to(self.root/'sys/devices/pci0000')
        for index,uid in enumerate(('testrootuuid','testdatauuid')):
            dm='LVM-testvguuid'+uid
            (self.root/f'dev/dm-{index}').write_text('preserved filesystem contents')
            (self.root/f'sys/class/block/dm-{index}/dm/uuid').write_text(dm)
            name='system--vg-'+('root' if index==0 else 'longhorn')
            (self.root/f'sys/class/block/dm-{index}/dm/name').write_text(name)
            (self.root/f'dev/mapper/{name}').symlink_to(self.root/f'dev/dm-{index}')
            (self.root/f'dev/disk/by-id/dm-uuid-{dm}').symlink_to(self.root/f'dev/dm-{index}')
        self.state={'os_size':64,'lh_size':128,'os_fs':64,'fs':'ext4'}
        (self.root/'state.json').write_text(json.dumps(self.state))
        code='#!'+sys.executable+'\n'+r'''
import json,os,sys
from pathlib import Path
GiB=1024**3;PE=4*1024**2
__INVENTORY__
def section(text,name,end=None):return text.split(name+'\n',1)[1].split('\n'+(end or 'END'+name),1)[0]
root=Path(os.environ['FIXTURE']);state=json.loads((root/'state.json').read_text())
cmd=Path(sys.argv[0]).name;args=sys.argv[1:]
raw=inventory(**state)
def save(): (root/'state.json').write_text(json.dumps(state))
def trace(text):
 with (root/'trace').open('a') as f:f.write(text+'\n')
def role(): return 'longhorn' if any('testdatauuid' in a or 'dm-1' in a or 'longhorn' in a for a in args) else 'os'
r=role();size=state['os_size' if r=='os' else 'lh_size'];fs_size=state['os_fs'] if r=='os' else state['lh_size']
if os.environ.get('FAIL_TOOL')==cmd:sys.exit(2)
if cmd=='unshare':
 assert args[:4]==['--mount','--propagation','private','--']
 os.environ['PRIVATE_MOUNTS']='yes';os.execvp(args[4],args[4:])
elif cmd=='lvs':
 if '--noheadings' in args:
  assert args==['--noheadings','--separator','|','-o','vg_name,vg_uuid,lv_uuid',str(root/'dev/mapper/system--vg-root')], 'Canonical dm-N is not an LVM selector'
  print('system-vg|test-vg-uuid|'+('other-root-uuid' if os.environ.get('MISMATCH_REPORT')=='yes' else 'test-root-uuid'))
  if os.environ.get('CHANGE_MAPPER')=='yes':
   alias=root/'dev/mapper/system--vg-root';alias.unlink();alias.symlink_to(root/'dev/dm-1')
 else:print(section(raw,'LVS'))
elif cmd=='vgs':print(section(raw,'VGS'))
elif cmd=='pvs':print(section(raw,'PVS'))
elif cmd=='mountpoint':sys.exit(0)
elif cmd=='findmnt':
 field=args[-1]
 print({'SOURCE':str(root/('dev/dm-0' if r=='os' else 'dev/dm-1')),'FSROOT':'/','OPTIONS':'rw','FSTYPE':state['fs'],
        'TARGET':'/\n'+str(root/'var/lib/longhorn')}[field])
elif cmd=='blkid':print('changed-fs' if (root/'changed-fs').exists() else 'fs-'+r+'-uuid')
elif cmd=='lsblk':
 if '-snbpP' in args:print(section(raw,'TOPO-'+r,'ENDTOPO'))
 elif '-snrpo' in args:print('/dev/dm-0 lvm\n/dev/nvme0n1p3 part\n/dev/nvme0n1 disk')
 else:print('253:'+('0' if r=='os' else '1'))
elif cmd=='tune2fs':print('Block size: 4096\nBlock count: '+str(fs_size*1024**3//4096))
elif cmd=='xfs_info':print('data = bsize=4096 blocks='+str(fs_size*1024**3//4096)+', imaxpct=25\nlog = internal bsize=4096\nrealtime = none extsz=4096')
elif cmd=='blockdev':print(state['os_size']*1024**3)
elif cmd=='lvextend':
 assert '--extents' in args and '--resizefs' not in args
 assert args[-2]==str(root/'dev/mapper/system--vg-root'), 'Growth must use the verified LVM selector'
 extent=int(args[args.index('--extents')+1]);assert extent*PE>size*1024**3
 state['os_size']=extent*PE//1024**3;save();trace('lv-grown')
 if os.environ.get('LATE_IDENTITY')=='yes':(root/'changed-fs').write_text('changed')
elif cmd in ('resize2fs','xfs_growfs'):
 assert os.environ.get('PRIVATE_MOUNTS')=='yes'
 assert args[0]=='/proc/self/fd/8' if cmd=='resize2fs' else args[-1]=='/proc/self/fd/7'
 if os.environ.get('RETARGET_AT_GROW')=='yes':
  alias=root/'dev/disk/by-id/dm-uuid-LVM-testvguuidtestrootuuid'
  alias.unlink();alias.symlink_to(root/'dev/dm-1')
  if cmd=='resize2fs':assert os.stat(args[0]).st_ino==os.stat(root/'dev/dm-0').st_ino
  else:assert os.stat(args[-1]).st_ino==os.stat('/').st_ino
 state['os_fs']=state['os_size'];save();trace('fs-grown')
else:raise AssertionError(cmd)
'''.replace('__INVENTORY__',inspect.getsource(inventory))
        for cmd in ('unshare','lvs','vgs','pvs','mountpoint','findmnt','blkid','lsblk','tune2fs','xfs_info','blockdev','lvextend','resize2fs','xfs_growfs'):
            path=self.root/'bin'/cmd;path.write_text(code);path.chmod(0o755)
        self.env={**os.environ,'FIXTURE':str(self.root),'PATH':str(self.root/'bin')+':'+os.environ['PATH']}
    def rebase(self,script):
        for source in ('/dev/disk/','/dev/mapper/','/var/lib/homestead','/var/lib/longhorn','/mnt/longhorn-os','/proc/sys/kernel/random/boot_id','/sys/class/block/'):
            script=script.replace(source,str(self.root)+source)
        return script.replace('[ -b "$D" ]','[ -f "$D" ]')
    def shell(self,script,**env):
        return subprocess.run(['sh','-c',self.rebase(script)],env={**self.env,**env},capture_output=True,text=True,timeout=30)
    def ref(self,request='a'*24,filesystem_only=False):
        state=json.loads((self.root/'state.json').read_text())
        facts=A.parse(inventory(**state))
        actual=self.shell(A.INSPECT)
        self.assertEqual(actual.returncode,0,actual.stdout+actual.stderr)
        digest=hashlib.sha256(('\n'.join(line for line in actual.stdout.splitlines() if not line.startswith('USAGE '))+'\n').encode()).hexdigest()
        return {**facts,'area':facts['areas'][0],'role':'os','target_bytes':80*1024**3,'request_id':request,'boot_id':'boot-test',
                'inspection_hash':digest,'filesystem_only':filesystem_only}
    def trace(self):
        path=self.root/'trace';return path.read_text() if path.exists() else ''
    def test_ext4_growth_and_receipt_are_absolute_and_never_repeated(self):
        ref=self.ref();result=self.shell(A._script(ref))
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(self.trace(),'lv-grown\nfs-grown\n')
        self.assertEqual((self.root/'dev/dm-1').read_text(),'preserved filesystem contents')
        self.assertEqual(json.loads((self.root/'state.json').read_text())['lh_size'],128)
        result=self.shell(A._script(ref));self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.trace(),'lv-grown\nfs-grown\n')
    def test_xfs_growth_uses_mounted_filesystem(self):
        self.state['fs']='xfs';(self.root/'state.json').write_text(json.dumps(self.state))
        result=self.shell(A._script(self.ref()));self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(self.trace(),'lv-grown\nfs-grown\n')
    def test_late_identity_stops_filesystem_write_after_extent_allocation(self):
        result=self.shell(A._script(self.ref()),LATE_IDENTITY='yes')
        self.assertNotEqual(result.returncode,0);self.assertEqual(self.trace(),'lv-grown\n')
    def test_filesystem_failure_can_finish_without_allocating_twice(self):
        result=self.shell(A._script(self.ref()),FAIL_TOOL='resize2fs')
        # resize2fs exists but fails only when run; inspecting its availability is read-only.
        self.assertNotEqual(result.returncode,0);self.assertEqual(self.trace(),'lv-grown\n')
        result=self.shell(A._script(self.ref('b'*24,filesystem_only=True)))
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(self.trace(),'lv-grown\nfs-grown\n')
    def test_changed_free_extents_and_failed_inventory_do_not_reach_growth(self):
        ref=self.ref();self.state['lh_size']=132;(self.root/'state.json').write_text(json.dumps(self.state))
        result=self.shell(A._script(ref));self.assertNotEqual(result.returncode,0);self.assertEqual(self.trace(),'')
        result=self.shell(A._script(ref),FAIL_TOOL='pvs');self.assertNotEqual(result.returncode,0);self.assertEqual(self.trace(),'')

    def test_lvm_report_mismatch_and_changed_mapper_alias_block_growth(self):
        ref=self.ref()
        for env in ({'MISMATCH_REPORT':'yes'},{'CHANGE_MAPPER':'yes'}):
            with self.subTest(env=env):
                result=self.shell(A._script(ref),**env)
                self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertEqual(self.trace(),'')

    def test_namespace_failure_never_grows_anything(self):
        result=self.shell(A._script(self.ref()),FAIL_TOOL='unshare')
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.trace(),'')

    def test_alias_retarget_at_filesystem_growth_cannot_change_the_pinned_device(self):
        for filesystem in ('ext4','xfs'):
            with self.subTest(filesystem=filesystem):
                self.state.update(fs=filesystem,os_size=64,os_fs=64)
                (self.root/'state.json').write_text(json.dumps(self.state))
                alias=self.root/'dev/disk/by-id/dm-uuid-LVM-testvguuidtestrootuuid'
                alias.unlink();alias.symlink_to(self.root/'dev/dm-0')
                result=self.shell(A._script(self.ref(request=('a' if filesystem=='ext4' else 'b')*24)),RETARGET_AT_GROW='yes')
                # Growth uses pinned descriptors; final inspection refuses the
                # changed alias, so the job requires explicit recovery review.
                self.assertNotEqual(result.returncode,0)
                self.assertEqual((self.root/'dev/dm-1').read_text(),'preserved filesystem contents')
                self.assertEqual(json.loads((self.root/'state.json').read_text())['lh_size'],128)


@unittest.skipUnless(sys.platform == 'linux', 'Linux mount namespaces')
class PrivateMountNamespaceTests(unittest.TestCase):
    def test_recursive_private_namespace_keeps_reviewed_nested_mount(self):
        # All bind mounts exist only inside the outer disposable namespace.
        # Unprivileged CI skips this kernel check; generated helper fixtures
        # above still require the exact namespace arguments and fail closed.
        check=subprocess.run(['unshare','--mount','--propagation','private','--','true'],capture_output=True)
        if check.returncode:self.skipTest('Mount namespaces are unavailable to this test user')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ('original/nested','alternate','target'):(root/name).mkdir(parents=True)
            (root/'original/nested/marker').write_text('reviewed')
            (root/'alternate/marker').write_text('other')
            script=r'''
set -eu
mount --bind "$T/original" "$T/target"
mount --bind "$T/original/nested" "$T/target/nested"
mount --make-rshared "$T/target"
unshare --mount --propagation private -- sh -c '
  set -eu
  [ "$(findmnt -n -o PROPAGATION --mountpoint "$T/target/nested")" = private ]
  touch "$T/ready"
  for i in $(seq 1 100); do [ ! -e "$T/go" ] || break; sleep .02; done
  [ -e "$T/go" ]
  [ "$(cat "$T/target/nested/marker")" = reviewed ]
' &
CHILD=$!
for i in $(seq 1 100); do [ ! -e "$T/ready" ] || break; sleep .02; done
[ -e "$T/ready" ]
mount --bind "$T/alternate" "$T/target/nested"
[ "$(cat "$T/target/nested/marker")" = other ]
touch "$T/go"
wait "$CHILD"
'''
            result=subprocess.run(['unshare','--mount','--propagation','private','--','sh','-c',script],env={**os.environ,'T':directory},capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__=='__main__':unittest.main()
