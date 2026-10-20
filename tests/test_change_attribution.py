"""Who changed an app travels on the write itself, so the leader credits a
change whichever copy of Homestead served it (#621)."""
import copy
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_auth as AUTH
import homestead_changes as CH
import homestead_kube as K
from fakes.kube import Kube

PATH = "/apis/apps/v1/namespaces/lab/deployments/sonarr"
AUTHOR_KEY = CH._author_key   # the real one, for the fresh-install test


def dep(image="example/sonarr:4.0"):
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"namespace": "lab", "name": "sonarr", "annotations": {"homestead.io/icon": "x"}},
            "spec": {"replicas": 1, "template": {"metadata": {"labels": {"app": "sonarr"}, "annotations": {"a": "b"}},
                                                 "spec": {"containers": [{"name": "app", "image": image}]}}}}


class AttributionTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        CH.bind(self.dir.name)
        CH._notes.clear()
        self.addCleanup(CH._notes.clear)
        self.addCleanup(setattr, CH.REQUEST, "user", None)
        # The account names the leader can tell apart, and the key it tells them by.
        for name, value in (("_author_key", lambda: b"test-key"),
                            ("_authors", lambda: {"users": ["james", "ci"], "api_keys": ["ci"]})):
            patcher = mock.patch.object(CH, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(K, "authorize_workload_write", lambda *args: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_through_another_copy(self, kube, user, body, method="PUT", ctype=None):
        """A write this copy serves, then the leader's own memory of notes, which is empty."""
        CH.REQUEST.user = user
        K.send(method, PATH, body, **({"ctype": ctype or "application/merge-patch+json"} if method == "PATCH" else {}))
        CH.REQUEST.user = None
        CH._notes.clear()
        return kube.object(PATH)

    def test_a_change_made_through_another_copy_is_credited_to_the_person(self):
        with Kube(dep()) as kube:
            start = time.time()
            CH.observe([kube.object(PATH)], now=start)
            stored = self.write_through_another_copy(kube, "james", dep(image="example/sonarr:4.1"))
            CH.observe([stored], now=time.time() + 1, since=start)
        entry = CH.history("lab", "sonarr")[0]
        self.assertEqual(("homestead", "james"), (entry["source"], entry["by"]))

    def test_the_mark_is_on_the_app_not_its_pods_and_does_not_name_the_person(self):
        with Kube(dep()) as kube:
            stored = self.write_through_another_copy(kube, "james", dep(image="example/sonarr:4.1"))
        self.assertEqual({"a": "b"}, stored["spec"]["template"]["metadata"]["annotations"],
                         "an annotation on the pod template would restart the app")
        mark = stored["metadata"]["annotations"].get(CH.AUTHOR)
        self.assertTrue(mark, "the write carries who made it")
        self.assertNotIn("james", mark, "the cluster object does not hold the account name")
        self.assertEqual("x", stored["metadata"]["annotations"]["homestead.io/icon"], "other annotations are kept")

    def test_a_merge_patch_carries_the_mark_and_keeps_its_own_fields(self):
        with Kube(dep()) as kube:
            stored = self.write_through_another_copy(kube, "api-key:ci", {"spec": {"replicas": 2}}, method="PATCH")
            self.assertEqual(2, stored["spec"]["replicas"])
            CH.observe([dep()], now=time.time() - 30)
            CH.observe([stored], now=time.time() + 1, since=time.time() - 30)
        entry = CH.history("lab", "sonarr")[0]
        self.assertEqual(("homestead", "api-key:ci"), (entry["source"], entry["by"]))

    def test_an_old_mark_does_not_claim_a_later_change(self):
        with Kube(dep()) as kube:
            stored = self.write_through_another_copy(kube, "james", dep(image="example/sonarr:4.1"))
        later = time.time() + 600                     # then someone runs kubectl edit
        CH.observe([stored], now=later - 60)
        changed = copy.deepcopy(stored)
        changed["spec"]["template"]["spec"]["containers"][0]["image"] = "example/sonarr:5.0"
        CH.observe([changed], now=later, since=later - 60)
        entry = CH.history("lab", "sonarr")[0]
        self.assertEqual(("outside", ""), (entry["source"], entry["by"]))

    def test_a_mark_the_leader_cannot_check_is_outside(self):
        # An account deleted since its edit cannot be told from a made-up mark.
        with Kube(dep()) as kube:
            start = time.time()
            CH.observe([kube.object(PATH)], now=start)
            stored = self.write_through_another_copy(kube, "since-deleted", dep(image="example/sonarr:4.1"))
            CH.observe([stored], now=time.time() + 1, since=start)
        entry = CH.history("lab", "sonarr")[0]
        self.assertEqual(("outside", ""), (entry["source"], entry["by"]))

    def test_a_job_of_homesteads_own_is_homestead_with_nobody(self):
        with Kube(dep()) as kube:
            start = time.time()
            CH.observe([kube.object(PATH)], now=start)
            stored = self.write_through_another_copy(kube, None, dep(image="example/sonarr:4.1"))
            CH.observe([stored], now=time.time() + 1, since=start)
        entry = CH.history("lab", "sonarr")[0]
        self.assertEqual(("homestead", ""), (entry["source"], entry["by"]))

    # Only Homestead's own write guard puts a mark on: one a request brings, say
    # in YAML from the resources editor, never reaches the cluster.
    def test_a_body_brought_with_someone_elses_mark_is_sent_with_ours(self):
        with Kube(dep()) as kube:
            start = time.time() - 30
            CH.observe([kube.object(PATH)], now=start)
            body = dep(image="example/sonarr:4.1")
            body["metadata"]["annotations"][CH.AUTHOR] = "brought by the request"
            stored = self.write_through_another_copy(kube, "james", body)
            self.assertNotEqual("brought by the request", stored["metadata"]["annotations"][CH.AUTHOR])
            CH.observe([stored], now=time.time() + 1, since=start)
        self.assertEqual(("homestead", "james"), (CH.history("lab", "sonarr")[0]["source"], CH.history("lab", "sonarr")[0]["by"]))

    def test_a_brought_mark_is_dropped_even_when_homestead_cannot_mark(self):
        CH.REQUEST.user = "james"
        body = dep(image="example/sonarr:4.1")
        body["metadata"]["annotations"][CH.AUTHOR] = "copied from earlier"
        with mock.patch.object(CH, "_author_key", mock.Mock(side_effect=LookupError("no key yet"))):
            CH.note_write("PUT", PATH, body)
        self.assertNotIn(CH.AUTHOR, body["metadata"]["annotations"])

    def test_a_json_patch_cannot_set_the_mark(self):
        CH.REQUEST.user = "james"
        pointer = "/metadata/annotations/" + CH.AUTHOR.replace("/", "~1")
        patch = [{"op": "add", "path": pointer, "value": "forged"},
                 {"op": "replace", "path": "/metadata/annotations", "value": {CH.AUTHOR: "forged", "keep": "me"}},
                 {"op": "replace", "path": "/metadata", "value": {"annotations": {CH.AUTHOR: "forged"}}},
                 {"op": "copy", "from": "/metadata/annotations/other", "path": pointer},
                 {"op": "replace", "path": "/spec/replicas", "value": 2}]
        CH.note_write("PATCH", PATH, patch)
        self.assertEqual([{"op": "replace", "path": "/metadata/annotations", "value": {"keep": "me"}},
                          {"op": "replace", "path": "/metadata", "value": {"annotations": {}}},
                          {"op": "replace", "path": "/spec/replicas", "value": 2}], patch)

    def test_a_json_patch_replacing_the_whole_app_cannot_bring_a_mark(self):
        CH.REQUEST.user = "james"
        whole = dep(image="example/sonarr:4.1")
        whole["metadata"]["annotations"][CH.AUTHOR] = "forged"
        patch = [{"op": "replace", "path": "", "value": whole}]
        CH.note_write("PATCH", PATH, patch)
        self.assertNotIn(CH.AUTHOR, patch[0]["value"]["metadata"]["annotations"])
        self.assertEqual("x", patch[0]["value"]["metadata"]["annotations"]["homestead.io/icon"])

    def test_a_json_patch_cannot_copy_or_move_into_the_apps_metadata(self):
        CH.REQUEST.user = "james"
        for op in ("copy", "move"):
            for target in ("", "/metadata", "/metadata/annotations"):
                with self.subTest(op=op, target=target):
                    patch = [{"op": op, "from": "/spec/template/metadata", "path": target}]
                    with self.assertRaisesRegex(ValueError, "copy or move"):
                        CH.note_write("PATCH", PATH, patch)
        allowed = [{"op": "copy", "from": "/metadata/labels/app", "path": "/metadata/labels/name"}]
        CH.note_write("PATCH", PATH, allowed)
        self.assertEqual(1, len(allowed), "a copy elsewhere is left alone")

    def test_bodies_that_cannot_carry_it_are_sent_unchanged(self):
        CH.REQUEST.user = "james"
        json_patch = [{"op": "replace", "path": "/spec/replicas", "value": 2}]
        scale = {"metadata": {"name": "sonarr"}, "spec": {"replicas": 0}}
        before = copy.deepcopy((json_patch, scale))
        CH.note_write("PATCH", PATH, json_patch)
        CH.note_write("PUT", PATH + "/scale", scale)
        self.assertEqual(before, (json_patch, scale), "a JSON patch or a Scale has no app metadata to mark")
        self.assertEqual(2, len(CH._notes), "both are still noted on this copy, as before")

    def test_a_delete_is_neither_noted_nor_an_entry(self):
        CH.REQUEST.user = "james"
        CH.note_write("DELETE", PATH, None)
        self.assertEqual([], CH._notes)
        CH.observe([dep()], now=1000)
        apps = CH.observe([], now=1060, since=1000)
        self.assertEqual([], apps["lab/sonarr"]["entries"], "an app that goes is not a change in its history")

    # A mark is trusted only for the app, the moment and (for a whole object)
    # the settings it was written with: anyone who can edit the cluster could
    # otherwise copy one to have their change credited to a Homestead user.
    def changed_with(self, mark, name="sonarr", image="example/sonarr:9.9"):
        out = dep(image=image)
        out["metadata"]["name"] = name
        out["metadata"]["annotations"][CH.AUTHOR] = mark
        return out

    def test_a_mark_copied_onto_another_app_is_not_trusted(self):
        with Kube(dep()) as kube:
            stored = self.write_through_another_copy(kube, "james", dep(image="example/sonarr:4.1"))
        start = time.time() - 30
        other = dep()
        other["metadata"]["name"] = "radarr"
        CH.observe([other], now=start)
        CH.observe([self.changed_with(stored["metadata"]["annotations"][CH.AUTHOR], name="radarr")],
                   now=time.time() + 1, since=start)
        entry = CH.history("lab", "radarr")[0]
        self.assertEqual(("outside", ""), (entry["source"], entry["by"]))

    def test_a_reservation_change_through_another_copy_is_credited(self):
        # As the reservations page sends it: only the pod template's containers.
        with Kube(dep()) as kube:
            start = time.time() - 30
            CH.observe([kube.object(PATH)], now=start)
            patch = {"spec": {"template": {"spec": {"containers": [
                {"name": "app", "resources": {"requests": {"cpu": "250m", "memory": "256Mi"}}}]}}}}
            stored = self.write_through_another_copy(kube, "james", patch, method="PATCH",
                                                     ctype="application/strategic-merge-patch+json")
            CH.observe([stored], now=time.time() + 1, since=start)
        entry = CH.history("lab", "sonarr")[0]
        self.assertEqual(("homestead", "james"), (entry["source"], entry["by"]))

    def test_a_write_the_api_server_rewrites_is_still_credited(self):
        with Kube(dep()) as kube:
            start = time.time() - 30
            CH.observe([kube.object(PATH)], now=start)
            body = dep()
            body["spec"]["template"]["spec"]["containers"][0]["resources"] = {"requests": {"cpu": "0.5"}}
            stored = self.write_through_another_copy(kube, "james", body)
        stored["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["cpu"] = "500m"   # as Kubernetes keeps it
        CH.observe([stored], now=time.time() + 1, since=start)
        entry = CH.history("lab", "sonarr")[0]
        self.assertEqual(("homestead", "james"), (entry["source"], entry["by"]))

    def test_a_user_and_an_api_key_of_the_same_name_are_told_apart(self):
        for user, credited in (("ci", "ci"), ("api-key:ci", "api-key:ci")):
            with self.subTest(user=user), Kube(dep()) as kube:
                CH._load().clear()
                start = time.time() - 30
                CH.observe([kube.object(PATH)], now=start)
                stored = self.write_through_another_copy(kube, user, dep(image=f"example/sonarr:{len(user)}"))
                CH.observe([stored], now=time.time() + 1, since=start)
                self.assertEqual(("homestead", credited), (CH.history("lab", "sonarr")[0]["source"], CH.history("lab", "sonarr")[0]["by"]))

    def test_looking_before_setup_makes_no_credentials(self):
        with mock.patch.object(CH, "_author_key", AUTHOR_KEY), \
                mock.patch.object(AUTH, "_load", return_value={}), mock.patch.object(AUTH, "_save") as save:
            CH.REQUEST.user = "james"
            body = dep(image="example/sonarr:4.1")
            CH.note_write("PUT", PATH, body)
            self.assertNotIn(CH.AUTHOR, body["metadata"]["annotations"], "no key yet: the write goes unmarked")
            start = time.time() - 30
            CH.observe([dep()], now=start)
            CH._notes.clear()
            marked = dep(image="example/sonarr:4.2")
            marked["metadata"]["annotations"][CH.AUTHOR] = json.dumps({"at": int(time.time()), "by": "0" * 32})
            CH.observe([marked], now=time.time() + 1, since=start)
        save.assert_not_called()
        self.assertEqual("outside", CH.history("lab", "sonarr")[0]["source"])

    def test_a_mark_whose_person_was_changed_is_not_trusted(self):
        with Kube(dep()) as kube:
            start = time.time() - 30
            CH.observe([kube.object(PATH)], now=start)
            stored = self.write_through_another_copy(kube, "james", dep(image="example/sonarr:4.1"))
        mark = json.loads(stored["metadata"]["annotations"][CH.AUTHOR])
        mark["by"] = "0" * len(mark["by"])
        tampered = copy.deepcopy(stored)
        tampered["metadata"]["annotations"][CH.AUTHOR] = json.dumps(mark)
        CH.observe([tampered], now=time.time() + 1, since=start)
        entry = CH.history("lab", "sonarr")[0]
        self.assertEqual(("outside", ""), (entry["source"], entry["by"]))

    def test_a_broken_mark_is_outside_and_the_other_apps_are_still_recorded(self):
        start = time.time() - 30
        radarr = dep()
        radarr["metadata"]["name"] = "radarr"
        CH.observe([dep(), radarr], now=start)
        for broken in ("[" * 50000, json.dumps({"at": int(time.time()), "by": "x" * 1000}), "{not json"):
            radarr_changed = copy.deepcopy(radarr)
            radarr_changed["spec"]["template"]["spec"]["containers"][0]["image"] = f"example/radarr:{len(broken)}"
            CH.observe([self.changed_with(broken, image=f"example/sonarr:{len(broken)}"), radarr_changed],
                       now=time.time() + 1, since=start)
            self.assertEqual(("outside", ""), (CH.history("lab", "sonarr")[0]["source"], CH.history("lab", "sonarr")[0]["by"]))
            self.assertEqual(f"example/radarr:{len(broken)}",
                             CH.history("lab", "radarr")[0]["changes"][0]["after"], "the next app is still recorded")


if __name__ == "__main__":
    unittest.main()
