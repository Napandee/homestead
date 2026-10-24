"""Disposable ext4 loop-image rehearsal, invoked in a private mount namespace.

Only the image created here is attached; all mount targets are under its own
temporary directory. Host identity/boot probes are substituted, never weakened
in the shipped worker. Mount/copy/checksum/retirement operations run for real.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import homestead_runtime_worker as W


class PowerLoss(Exception): pass


def rehearse(interrupt=''):
    with tempfile.TemporaryDirectory(prefix='homestead-runtime-test-') as temporary:
        root = Path(temporary)
        image = root / 'source.img'
        with image.open('wb') as output: output.truncate(96 * 1024 ** 2)
        subprocess.run(['mkfs.ext4','-q','-F',str(image)],check=True)
        device = subprocess.check_output(['losetup','--find','--show',str(image)],text=True).strip()
        source, retained, base = root/'runtime', root/'retained', root/'maintenance'
        source.mkdir(); retained.mkdir(); base.mkdir()
        folder=base/('a'*24);folder.mkdir()
        real_run=W.run
        fstab=root/'fstab'
        mounted=[]
        interrupted=False
        try:
            real_run('mount',device,str(source)); mounted.append(source)
            uuid=real_run('blkid','-s','UUID','-o','value',device)
            (source/'longhorn-disk.cfg').write_text('{"diskUUID":"example-disk"}')
            (source/'replicas').mkdir()
            (source/'engine-binaries').mkdir()
            (source/'engine-binaries/engine').write_bytes(b'verified runtime\n'*1024)
            (source/'metadata').mkdir()
            (source/'metadata/state').write_bytes(b'persistent metadata')
            rootfs=real_run('findmnt','-rn','--target',str(base),'-o','FSTYPE')
            original=f'UUID={uuid} {source} ext4 defaults 0 0\n'
            fstab.write_text(original)
            plan={'hardware':{'by_id':device,'uuid':uuid,'fstype':'ext4','size_bytes':image.stat().st_size},
                  'root':{'uuid':'test-root','filesystem':rootfs},'disk_uuid':'example-disk',
                  'request_id':'a'*24,'retained':str(retained),'fstab':original,'fstab_metadata':W.metadata(fstab),'boot_id':'previous-boot'}
            W.durable(folder/'plan.json',json.dumps(plan));W.save(folder,{'phase':'staged'})
            def command(*args):
                nonlocal interrupted
                if args[0]=='systemctl':return ''
                if args[0]=='mount' and len(args)==2:
                    mounted.append(Path(args[1]))
                    result=real_run('mount','-T',str(fstab),args[1])
                else: result=real_run(*args)
                boundary = ('unmounted' if args == ('umount',str(source)) else
                            'retained-mounted' if args == ('mount',str(retained)) else
                            'runtime-bound' if args == ('mount',str(source)) else '')
                if boundary and boundary == interrupt and not interrupted:
                    interrupted=True
                    raise PowerLoss(boundary)
                return result
            def save(target,state):
                W.durable(target/'state.json',json.dumps(state))
                if state['phase']==interrupt: raise PowerLoss(interrupt)
            with patch.multiple(W,BASE=base,SOURCE=source,FSTAB=fstab,RESERVE=0), \
                    patch.object(W,'run',side_effect=command),patch.object(W,'identity',return_value=device), \
                    patch.object(W,'offline'),patch.object(W,'root_identity',return_value=plan['root']):
                if interrupt in ('copying','cutover','relocated','unmounted','retained-mounted','runtime-bound'):
                    with patch.object(W,'save',side_effect=save):
                        try: W.boot(folder)
                        except PowerLoss: pass
                if interrupt=='fstab-written':
                    durable=W.durable
                    def rewritten(path,content):
                        durable(path,content)
                        if path==fstab:raise PowerLoss('fstab-written')
                    with patch.object(W,'durable',side_effect=rewritten):
                        try:W.boot(folder)
                        except PowerLoss:pass
                if interrupt=='mid-copy':
                    copier=W.copy_runtime
                    def partial_copy(src,dst,expected):
                        dst.mkdir();(dst/'engine-binaries').mkdir()
                        (dst/'engine-binaries/engine').write_bytes((src/'engine-binaries/engine').read_bytes()[:7])
                        raise PowerLoss('mid-copy')
                    with patch.object(W,'copy_runtime',side_effect=partial_copy):
                        try:W.boot(folder)
                        except PowerLoss:pass
                    assert (folder/'active/engine-binaries/engine').stat().st_size==7
                if interrupt=='copy-capacity':
                    copier=W.copy_runtime
                    def complete_one(src,dst,expected):
                        copier(src,dst,expected)
                        raise PowerLoss('copy-capacity')
                    with patch.object(W,'copy_runtime',side_effect=complete_one):
                        try:W.boot(folder)
                        except PowerLoss:pass
                    remaining=sum(i.get('size',0) for i in W.read(folder/'state.json')['manifest'].values())
                    with patch.object(W.os,'statvfs',return_value=SimpleNamespace(f_bavail=remaining,f_frsize=1)):
                        state=W.boot(folder)
                else:state=W.boot(folder)
                assert state['phase']=='relocated',state
                assert (retained/'engine-binaries/engine').exists()
                assert not (source/'longhorn-disk.cfg').exists()
                assert (folder/'archive/engine-binaries/engine').read_bytes()==(source/'engine-binaries/engine').read_bytes()
                assert 'ro' in W.mount(retained,'OPTIONS').split(',')
                if interrupt=='changed-source':
                    real_run('mount','-o','remount,rw',str(retained))
                    (retained/'engine-binaries/extra').write_text('not archived')
                    real_run('mount','-o','remount,ro',str(retained))
                    try: W.arm_retirement(folder)
                    except ValueError: pass
                    else: raise AssertionError('Changed source was accepted')
                    assert (retained/'engine-binaries/extra').read_text()=='not archived'
                    return
                if interrupt=='changed-archive':
                    (folder/'archive/engine-binaries/engine').write_text('corruption')
                    try: W.arm_retirement(folder)
                    except ValueError: pass
                    else: raise AssertionError('Changed archive was accepted')
                    assert (retained/'engine-binaries/engine').exists()
                    return
                W.arm_retirement(folder)
                state=W.read(folder/'state.json');state['armed_boot']='previous-retirement-boot';W.save(folder,state)
                if interrupt=='retiring':
                    with patch.object(W,'save',side_effect=save):
                        try: W.boot(folder)
                        except PowerLoss: pass
                elif interrupt=='unlink':
                    unlink=Path.unlink
                    def interrupted_unlink(path,*args,**kwargs):
                        result=unlink(path,*args,**kwargs)
                        if str(path).startswith(str(retained)) and path.name=='engine':raise PowerLoss('unlink')
                        return result
                    with patch.object(Path,'unlink',interrupted_unlink):
                        try: W.boot(folder)
                        except PowerLoss: pass
                state=W.boot(folder)
                assert state['phase']=='retired',state
                assert (retained/'longhorn-disk.cfg').exists()
                assert not (retained/'engine-binaries').exists()
                assert (folder/'archive/engine-binaries/engine').exists()
                assert (source/'engine-binaries/engine').exists()
                assert real_run('blkid','-s','UUID','-o','value',device)==uuid
                assert W.boot(folder)['phase']=='retired'
        finally:
            for point in [source,retained]:
                while str(point) in real_run('findmnt','-rn','-o','TARGET').splitlines():
                    subprocess.run(['umount',str(point)],check=True)
            subprocess.run(['losetup','-d',device],check=True)


if __name__=='__main__':
    rehearse(sys.argv[1] if len(sys.argv)>1 else '')
    print('Disposable loop-image rehearsal passed')
