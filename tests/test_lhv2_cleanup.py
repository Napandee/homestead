import copy
from contextlib import nullcontext
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
import homestead_lhv2_cleanup as C
import homestead_capacity_review as REVIEW
import homestead_storage_conflicts as CONFLICTS


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.setting={'metadata':{'uid':'setting-uid','resourceVersion':'3'},'value':'true'}
        self.node={'metadata':{'name':'host-1','uid':'node-uid','resourceVersion':'7'},'spec':{},
                   'status':{'nodeInfo':{'bootID':'boot-1'},'conditions':[{'type':'Ready','status':'True'}], 'capacity':{'hugepages-2Mi':'2Gi'}}}
        self.rows={C.CAP.LH+'/volumes':[],C.CAP.LH+'/nodes':[],C.CAP.LH+'/instancemanagers':[], '/api/v1/nodes':[self.node],'/api/v1/pods':[]}
        self.memory={'boot':'boot-1','pages':1024,'free':1024,'reserved':0,'owned_pages':512,'baseline':512,'phase':'prepared','proof':'proof','reason':''}
        self.saved=[]; self.writes=[]; self.host_writes=[]
        def start(kind,title,resource,href,ref,message):
            op={'id':'cleanup-1','kind':kind,'status':'running','ref':ref,'message':message,'progress':0}; self.saved.append(op); return op
        ops=SimpleNamespace(stored=lambda:self.saved,transaction=nullcontext,TERMINAL={'failed','succeeded','cancelled'},start=start,checkpoint=lambda item:None,_public=lambda item:{k:v for k,v in item.items() if k!='ref'})
        def send(method,path,body,**kwargs):
            self.writes.append((path,body));
            if path.endswith('v2-data-engine'): self.setting['value']='false'
            if path.endswith('host-1'): self.node['spec']['unschedulable']=True
        def host(row,action='inspect',**kwargs):
            if action=='stage':
                self.host_writes.append(kwargs); self.memory['phase']='restart-required'
            return copy.deepcopy(self.memory)
        for obj,name,value in [(C.SETUP,'ops',ops),(C.SETUP,'kget',lambda path:self.setting),(C.SETUP,'ksend',send),
                              (C.CAP,'ksend',send),(C.KUBE,'get_optional',lambda *a,**k:None),
                              (C,'items',lambda path:copy.deepcopy(self.rows[path])),(C,'host',host),(REVIEW,'_key',lambda:b'test-key')]:
            p=patch.object(obj,name,value);p.start();self.addCleanup(p.stop)

    def start(self):
        plan=C.review(); return C.start({k:plan[k] for k in ('request_id','capacity_token')}|{'confirm_capacity':True})

    def test_disable_waits_for_managers_then_cordons_and_stages_only_owned_pages(self):
        self.rows[C.CAP.LH+'/instancemanagers']=[{'metadata':{'name':'manager-1'},'spec':{'dataEngine':'v2'}}]
        self.start(); item=self.saved[0]
        C.progress(item); self.assertEqual('false',self.setting['value']); self.assertFalse(self.host_writes)
        C.progress(item); self.assertFalse(self.host_writes)
        self.rows[C.CAP.LH+'/instancemanagers']=[]
        self.rows['/api/v1/pods']=[{'metadata':{'name':'manager-1','namespace':'longhorn-system'},'spec':{}}]
        C.progress(item); self.assertFalse(self.host_writes)
        self.rows['/api/v1/pods']=[]; C.progress(item)
        self.assertTrue(self.node['spec']['unschedulable']); self.assertEqual(1,len(self.host_writes))
        self.assertEqual('restart-required',item['ref']['phase'])
        C.progress(item); self.assertEqual(1,len(self.host_writes))
        self.memory.update(phase='complete',boot='boot-2',pages=512)
        self.assertEqual('running',C.progress(item)[0])
        self.node['status']['capacity']['hugepages-2Mi']='1Gi'
        self.assertEqual('running',C.progress(item)[0])
        self.node['status']['nodeInfo']['bootID']='boot-2'
        self.assertEqual('succeeded',C.progress(item)[0])
        self.assertTrue(self.node['spec']['unschedulable'])

    def test_changed_ownership_or_confirmation_never_starts(self):
        plan=C.review(); body={k:plan[k] for k in ('request_id','capacity_token')}
        with self.assertRaises(ValueError):C.start(body)
        body['confirm_capacity']=True; self.memory['proof']='changed'
        with self.assertRaises(ValueError):C.start(body)
        self.assertFalse(self.saved); self.assertFalse(self.writes)

    def test_v2_data_disks_or_setup_task_blocks_disable(self):
        for kind in ('volume','disk','task'):
            with self.subTest(kind=kind):
                if kind=='volume':self.rows[C.CAP.LH+'/volumes']=[{'spec':{'dataEngine':'v2'}}]
                if kind=='disk':self.rows[C.CAP.LH+'/nodes']=[{'spec':{'disks':{'data':{'diskType':'block'}}}}]
                if kind=='task':self.saved.append({'kind':'longhorn-v2-prepare','status':'running'})
                with self.assertRaises(ValueError):C.review()
                self.rows[C.CAP.LH+'/volumes']=[];self.rows[C.CAP.LH+'/nodes']=[];self.saved.clear()
        self.assertFalse(self.writes)

    def test_unknown_legacy_reservations_disable_cpu_without_cordon_or_memory_writes(self):
        self.memory.update(phase='retained',owned_pages=0,reason='No ownership record')
        self.start();item=self.saved[0];C.progress(item)
        result=C.progress(item)
        self.assertEqual('succeeded',result[0]);self.assertIn('retained',result[2]);self.assertFalse(self.host_writes)
        self.assertNotIn('unschedulable',self.node['spec'])

    def test_other_hugepage_workloads_or_reenabled_engine_prevent_host_write(self):
        self.start();item=self.saved[0];C.progress(item)
        self.rows['/api/v1/pods']=[{'metadata':{'name':'other','namespace':'apps'},'spec':{'nodeName':'host-1','containers':[{'resources':{'requests':{'hugepages-2Mi':'2Gi'}}}]}}]
        with self.assertRaisesRegex(ValueError,'Other workloads'):C.progress(item)
        self.assertFalse(self.host_writes);self.assertNotIn('unschedulable',self.node['spec'])
        self.setting['value']='true'
        with self.assertRaisesRegex(ValueError,'re-enabled'):C.progress(item)

    def test_recovery_blocks_new_preparation_and_allows_only_reviewed_reboot(self):
        self.start();item=self.saved[0];C.progress(item);C.progress(item)
        with self.assertRaises(ValueError):C.require_idle()
        with self.assertRaises(ValueError):CONFLICTS.require_clear(self.saved,'host-os',{'node':'host-1'})
        CONFLICTS.require_clear(self.saved,'node-power',{'node':'host-1','action':'reboot'})
        with self.assertRaises(ValueError):CONFLICTS.require_clear(self.saved,'node-power',{'node':'host-1','action':'shutdown'})

    def test_new_node_identity_and_partial_inventory_refuse(self):
        self.start();item=self.saved[0];C.progress(item);self.node['metadata']['uid']='replaced'
        with self.assertRaises(ValueError):C.progress(item)
        self.assertFalse(self.host_writes)
        self.saved.clear()
        with patch.object(C,'items',side_effect=ValueError('Incomplete inventory')),self.assertRaises(ValueError):C.review()

    def test_failed_unresolved_disk_maintenance_still_blocks_cleanup(self):
        for kind in ('longhorn-runtime', 'disk-allocation', 'disk-v2-convert'):
            with self.subTest(kind=kind):
                self.saved[:]=[{'kind':kind,'status':'failed','ref':{'phase':'staged'}}]
                with self.assertRaisesRegex(ValueError,'maintenance'):C.review()
        self.assertFalse(self.writes)

    def test_offline_host_never_counts_as_resource_release(self):
        self.memory.update(phase='retained',owned_pages=0)
        self.start();item=self.saved[0];C.progress(item)
        self.node['status']['conditions'][0]['status']='False'
        result=C.progress(item)
        self.assertEqual('running',result[0]);self.assertIn('unavailable',result[2])

    def test_late_workload_after_cordon_prevents_boot_policy_write(self):
        self.start();item=self.saved[0];C.progress(item)
        original=C.SETUP.ksend
        def send(*args,**kwargs):
            result=original(*args,**kwargs)
            self.rows['/api/v1/pods']=[{'metadata':{'name':'late','namespace':'apps'},'spec':{'nodeName':'host-1','containers':[{'resources':{'requests':{'hugepages-2Mi':'2Gi'}}}]}}]
            return result
        with patch.object(C.SETUP,'ksend',side_effect=send),self.assertRaisesRegex(ValueError,'claimed hugepages'):C.progress(item)
        self.assertFalse(self.host_writes);self.assertTrue(self.node['spec']['unschedulable'])
