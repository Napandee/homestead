import sys, unittest, urllib.error
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server
import homestead_storage as STORAGE

DEPLOYMENTS = "/apis/apps/v1/deployments"
STATEFULSETS = "/apis/apps/v1/statefulsets"
VMS = "/apis/kubevirt.io/v1/virtualmachines"
CRONJOBS = "/apis/batch/v1/cronjobs"
JOBS = "/apis/batch/v1/jobs"
PODS = "/api/v1/pods"
REPLICASETS = "/apis/apps/v1/replicasets"
CONTROLLERS = "/api/v1/replicationcontrollers"


def deployment(name, claim, ns="lab"):
    return {"metadata": {"namespace": ns, "name": name},
            "spec": {"template": {"spec": {"volumes": [{"name": "d", "persistentVolumeClaim": {"claimName": claim}}]}}}}


def owned(kind, uid, name="owner"):
    return [{"apiVersion": "v1", "kind": kind, "name": name, "uid": uid, "controller": True}]


def pod(name, claim, ns="lab", owners=None, phase="Running", uid=None):
    meta = {"namespace": ns, "name": name, "uid": uid or f"uid-{name}"}
    if owners:
        meta["ownerReferences"] = owners
    return {"metadata": meta, "status": {"phase": phase},
            "spec": {"volumes": [{"name": "d", "persistentVolumeClaim": {"claimName": claim}}]}}


def job(name, claim, ns="lab", owners=None, uid=None):
    meta = {"namespace": ns, "name": name, "uid": uid or f"uid-{name}"}
    if owners:
        meta["ownerReferences"] = owners
    return {"metadata": meta, "spec": {"template": pod(name, claim, ns)}}


def replicas(name, claim, ns="lab", owners=None, uid=None):
    """A ReplicaSet or ReplicationController: both carry a pod template."""
    return job(name, claim, ns, owners, uid)


class Lists:
    """Each kind's list: a dict answer, or an exception to raise."""

    def __init__(self, **answers):
        self.answers = {DEPLOYMENTS: {"items": [deployment("plex", "plex-config")]}}
        self.answers.update(answers)

    def __call__(self, path, **kw):
        answer = self.answers.get(path, {"items": []})
        if isinstance(answer, Exception):
            raise answer
        return answer


def forbidden(path):
    return urllib.error.HTTPError(path, 403, "forbidden", {}, None)


class ClaimReferencesTests(unittest.TestCase):
    """The Volumes page is where people decide what to delete. A kind whose
    list could not be read must not make a claim look unused, and must not
    hide what the kinds that were read say either."""

    def users(self, lists, claim, ns="lab"):
        with patch.object(STORAGE, "kget", lists):
            return STORAGE._references_for(STORAGE.claim_references(), ns, claim)

    def test_a_claim_found_in_a_read_kind_is_named_when_another_kind_failed(self):
        self.assertEqual(["Deployment/plex"], self.users(Lists(**{STATEFULSETS: forbidden(STATEFULSETS)}), "plex-config"))

    def test_a_claim_nothing_read_refers_to_is_unknown_when_a_kind_failed(self):
        self.assertIsNone(self.users(Lists(**{STATEFULSETS: forbidden(STATEFULSETS)}), "old-data"))

    def test_a_list_without_items_is_not_an_empty_list(self):
        self.assertIsNone(self.users(Lists(**{STATEFULSETS: {}}), "old-data"))

    def test_a_list_cut_short_is_not_the_whole_list(self):
        cut = {"metadata": {"continue": "token"}, "items": []}
        self.assertIsNone(self.users(Lists(**{STATEFULSETS: cut}), "old-data"))

    def test_a_list_with_invalid_rows_is_not_read(self):
        self.assertIsNone(self.users(Lists(**{STATEFULSETS: {"items": ["not an object"]}}), "old-data"))

    def test_a_list_whose_metadata_is_not_an_object_is_not_read(self):
        self.assertIsNone(self.users(Lists(**{STATEFULSETS: {"metadata": "x", "items": []}}), "old-data"))

    def test_an_answer_that_is_not_an_object_is_not_read(self):
        self.assertIsNone(self.users(Lists(**{STATEFULSETS: ["not", "a", "list", "object"]}), "old-data"))

    def test_a_malformed_row_makes_its_kind_unread_without_hiding_the_others(self):
        malformed = {
            "a StatefulSet without metadata": {STATEFULSETS: {"items": [{"spec": {}}]}},
            "a Deployment without a pod template": {DEPLOYMENTS: {"items": [
                deployment("plex", "plex-config"), {"metadata": {"namespace": "lab", "name": "broken"}}]}},
            "a VM without metadata": {VMS: {"items": [{"spec": {}}]}},
            "a VM whose volumes are not a list of objects": {VMS: {"items": [
                {"metadata": {"namespace": "lab", "name": "vm1"}, "spec": {"template": {"spec": {"volumes": ["x"]}}}}]}},
        }
        for what, answers in malformed.items():
            with self.subTest(what):
                lists = Lists(**answers)
                self.assertEqual(["Deployment/plex"], self.users(lists, "plex-config"))
                self.assertIsNone(self.users(lists, "old-data"))

    def test_kubevirt_absent_means_no_vms(self):
        self.assertEqual([], self.users(Lists(**{VMS: urllib.error.HTTPError(VMS, 404, "missing", {}, None)}), "old-data"))

    def test_vms_that_could_not_be_read_are_unknown(self):
        self.assertIsNone(self.users(Lists(**{VMS: urllib.error.HTTPError(VMS, 500, "boom", {}, None)}), "old-data"))

    def test_with_everything_read_a_claim_nothing_uses_is_unused(self):
        self.assertEqual([], self.users(Lists(), "old-data"))


class BareWorkloadTests(unittest.TestCase):
    """A Job, Pod, ReplicaSet or ReplicationController made on its own, with
    nothing above it, is what uses its claim: without it the claim read as
    orphaned and could be deleted (gap 12). One made by something that was
    read is already named by that owner."""

    def users(self, claim, **answers):
        with patch.object(STORAGE, "kget", Lists(**answers)):
            return STORAGE._references_for(STORAGE.claim_references(), "lab", claim)

    def test_a_bare_job_names_its_claim(self):
        self.assertEqual(["Job/migrate"], self.users("db-dump", **{JOBS: {"items": [job("migrate", "db-dump")]}}))

    def test_a_bare_pod_names_its_claim_running_or_finished(self):
        for phase in ("Running", "Succeeded", "Failed", "Pending"):
            with self.subTest(phase):
                self.assertEqual(["Pod/debug"], self.users("scratch", **{PODS: {"items": [pod("debug", "scratch", phase=phase)]}}))

    def test_a_bare_replicaset_and_replication_controller_name_their_claims(self):
        self.assertEqual(["ReplicaSet/web"], self.users("web-data", **{REPLICASETS: {"items": [replicas("web", "web-data")]}}))
        self.assertEqual(["ReplicationController/legacy"],
                         self.users("legacy-data", **{CONTROLLERS: {"items": [replicas("legacy", "legacy-data")]}}))

    def test_what_a_read_owner_made_is_named_by_that_owner_only(self):
        cron = {"metadata": {"namespace": "lab", "name": "backup", "uid": "uid-cron"},
                "spec": {"jobTemplate": {"spec": {"template": pod("t", "backup-data")}}}}
        deploy = deployment("plex", "plex-config")
        deploy["metadata"]["uid"] = "uid-plex"
        answers = {
            CRONJOBS: {"items": [cron]},
            DEPLOYMENTS: {"items": [deploy]},
            JOBS: {"items": [job("backup-1", "backup-data", owners=owned("CronJob", "uid-cron"))]},
            REPLICASETS: {"items": [replicas("plex-6d9", "plex-config", owners=owned("Deployment", "uid-plex"))]},
            PODS: {"items": [pod("backup-1-x", "backup-data", owners=owned("Job", "uid-backup-1")),
                             pod("plex-6d9-y", "plex-config", owners=owned("ReplicaSet", "uid-plex-6d9"))]},
        }
        self.assertEqual(["CronJob/backup"], self.users("backup-data", **answers))
        self.assertEqual(["Deployment/plex"], self.users("plex-config", **answers))

    def test_a_job_whose_owner_was_not_read_is_named(self):
        """Its owner is gone, or of a kind not read (an operator's own
        resource): nothing else would name the claim."""
        self.assertEqual(["Job/orphan"], self.users("old-run", **{JOBS: {"items": [
            job("orphan", "old-run", owners=owned("CronJob", "uid-deleted"))]}}))
        self.assertEqual(["Pod/db-0"], self.users("db-data", **{PODS: {"items": [
            pod("db-0", "db-data", owners=owned("Cluster", "uid-operator-cr"))]}}))

    def test_a_vms_launcher_pod_is_named_by_the_vm_and_a_lone_instance_s_by_itself(self):
        vm = {"metadata": {"namespace": "lab", "name": "win11", "uid": "uid-vm"},
              "spec": {"template": {"spec": {"volumes": [{"name": "d", "persistentVolumeClaim": {"claimName": "win11-disk"}}]}}}}
        answers = {VMS: {"items": [vm]}, PODS: {"items": [
            pod("virt-launcher-win11-abcde", "win11-disk", owners=owned("VirtualMachineInstance", "uid-vmi", "win11")),
            pod("virt-launcher-lone-fghij", "lone-disk", owners=owned("VirtualMachineInstance", "uid-lone", "lone"))]}}
        self.assertEqual(["VM/win11"], self.users("win11-disk", **answers))
        self.assertEqual(["Pod/virt-launcher-lone-fghij"], self.users("lone-disk", **answers))

    def test_a_claim_the_owner_no_longer_names_is_named_by_what_still_mounts_it(self):
        """The owner's template moved on (a CronJob edited, a Deployment rolled
        out, a volume hotplugged into a VM or added by a webhook), but what it
        made still mounts the old claim: that claim is in use."""
        cron = {"metadata": {"namespace": "lab", "name": "backup", "uid": "uid-cron"},
                "spec": {"jobTemplate": {"spec": {"template": pod("t", "backup-new")}}}}
        deploy = deployment("plex", "plex-new")
        deploy["metadata"]["uid"] = "uid-plex"
        answers = {
            CRONJOBS: {"items": [cron]},
            DEPLOYMENTS: {"items": [deploy]},
            JOBS: {"items": [job("backup-1", "backup-old", owners=owned("CronJob", "uid-cron", "backup"))]},
            REPLICASETS: {"items": [replicas("plex-old", "plex-old-data", owners=owned("Deployment", "uid-plex", "plex"))]},
            PODS: {"items": [pod("backup-1-x", "backup-old", owners=owned("Job", "uid-backup-1", "backup-1")),
                             pod("plex-old-y", "plex-old-data", owners=owned("ReplicaSet", "uid-plex-old", "plex-old")),
                             pod("hp-volume-z", "hotplugged", owners=owned("Pod", "uid-launcher", "virt-launcher-vm1"))]},
        }
        self.assertEqual(["Job/backup-1"], self.users("backup-old", **answers))
        self.assertEqual(["ReplicaSet/plex-old"], self.users("plex-old-data", **answers))
        self.assertEqual(["Pod/hp-volume-z"], self.users("hotplugged", **answers))

    def test_a_statefulset_s_own_claims_are_not_named_again_by_its_pods(self):
        sts = {"metadata": {"namespace": "lab", "name": "db", "uid": "uid-db"},
               "spec": {"template": {"spec": {}}, "volumeClaimTemplates": [{"metadata": {"name": "data"}}]}}
        answers = {STATEFULSETS: {"items": [sts]},
                   PODS: {"items": [pod("db-0", "data-db-0", owners=owned("StatefulSet", "uid-db", "db"))]}}
        self.assertEqual(["StatefulSet/db"], self.users("data-db-0", **answers))

    def test_each_new_kind_unreadable_leaves_unused_claims_unknown(self):
        for path in (JOBS, PODS, REPLICASETS, CONTROLLERS):
            for answer in (forbidden(path), {"metadata": {"continue": "t"}, "items": []}, {"items": [{"spec": {}}]}):
                with self.subTest(path=path, answer=answer):
                    self.assertIsNone(self.users("old-data", **{path: answer}))

    def test_a_pod_list_already_read_is_used_and_not_read_again(self):
        """The page snapshot's cycle reads the pods once for every part."""
        read = []
        lists = Lists()

        def get(path, **kw):
            read.append(path)
            return lists(path, **kw)
        with patch.object(STORAGE, "kget", get):
            refs = STORAGE.claim_references(pods={"items": [pod("debug", "scratch")], "metadata": {}})
        self.assertNotIn(PODS, read)
        self.assertEqual(["Pod/debug"], STORAGE._references_for(refs, "lab", "scratch"))

    def test_with_everything_read_a_claim_nothing_uses_is_still_unused(self):
        self.assertEqual([], self.users("old-data", **{PODS: {"items": [pod("debug", "scratch")]}}))


class VolumesPageTests(unittest.TestCase):
    def setUp(self):
        self.lists = Lists(**{STATEFULSETS: forbidden(STATEFULSETS)})
        self.claims = {("lab", name): {"metadata": {"namespace": "lab", "name": name}, "spec": {"volumeName": f"pvc-{name}"}}
                       for name in ("plex-config", "old-data")}

    def get(self, path, **kw):
        if path.endswith("/volumes"):
            return {"items": [{"metadata": {"name": f"pvc-{name}"}, "spec": {"size": "1073741824"},
                               "status": {"state": "detached", "kubernetesStatus": {"namespace": "lab", "pvcName": name}}}
                              for name in ("plex-config", "old-data")]}
        if path == "/api/v1/persistentvolumeclaims":
            return {"items": list(self.claims.values())}
        return self.lists(path, **kw)

    def test_one_unreadable_kind_leaves_known_users_shown_and_the_rest_unknown(self):
        with patch.object(STORAGE, "kget", self.get), \
             patch.object(server.CACHE, "cached", side_effect=lambda key, ttl, fn: fn()):
            rows = {row["pvc_name"]: row for row in STORAGE.get_volumes()}
        self.assertEqual(["Deployment/plex"], rows["plex-config"]["used_by"])
        self.assertIsNone(rows["old-data"]["used_by"])


class EnsureClaimTests(unittest.TestCase):
    """Installing over a volume that is already there takes it only when
    nothing uses it; not knowing what uses it is not "nothing"."""

    def ensure(self, lists):
        def get(path, **kw):
            if path == "/api/v1/namespaces/lab/persistentvolumeclaims/old-data":
                return {"metadata": {"name": "old-data"}}
            return lists(path, **kw)
        with patch.object(STORAGE, "kget", get), patch.object(server.STORAGE_ROUTES, "kget", get):
            return server.STORAGE_ROUTES.ensure_claim("lab", "old-data", 5)

    def test_an_existing_volume_is_not_taken_when_what_uses_it_could_not_be_checked(self):
        with self.assertRaisesRegex(ValueError, "could not check what uses it"):
            self.ensure(Lists(**{STATEFULSETS: forbidden(STATEFULSETS)}))

    def test_an_existing_volume_nothing_uses_is_taken(self):
        self.assertEqual("old-data", self.ensure(Lists()))


if __name__ == "__main__":
    unittest.main()
