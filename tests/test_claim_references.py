import sys, unittest, urllib.error
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server
import homestead_storage as STORAGE

DEPLOYMENTS = "/apis/apps/v1/deployments"
STATEFULSETS = "/apis/apps/v1/statefulsets"
VMS = "/apis/kubevirt.io/v1/virtualmachines"


def deployment(name, claim, ns="lab"):
    return {"metadata": {"namespace": ns, "name": name},
            "spec": {"template": {"spec": {"volumes": [{"name": "d", "persistentVolumeClaim": {"claimName": claim}}]}}}}


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
