"""#735: a change made in Homestead as YAML to commit - only the fields that
changed, under the Deployment's kind, name and namespace."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_gitops as GITOPS
import homestead_updates as UPDATES
import homestead_yaml as YAML


def deployment():
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "web", "namespace": "lab", "resourceVersion": "9", "uid": "u",
                         "labels": {"app": "web"}, "annotations": {UPDATES.RAN: "{}", "homestead.io/group": "Home"}},
            "spec": {"replicas": 1, "template": {"metadata": {"labels": {"app": "web"}, "annotations": {}},
                     "spec": {"containers": [
                         {"name": "main", "image": "example/web:1", "env": [{"name": "A", "value": "1"}, {"name": "B", "value": "x"}],
                          "resources": {"requests": {"memory": "256Mi"}}},
                         {"name": "side", "image": "example/side:1"}]}}},
            "status": {"replicas": 1}}


class Writer(unittest.TestCase):
    def test_round_trip_keeps_strings(self):
        tricky = ["1", "0755", "1.5", "1e3", "0x1F", "true", "yes", "on", "off", "no", "y", "null", "~", "",
                  "a: b", "#x", "x #y", "- x", "[x]", "{x}", "*x", "&x", "!x", "|", ">", "'q'", '"q"', "%x", "@x", "`x",
                  " lead", "trail ", "two\nlines", "1:30", ".inf", "250m", "1104Mi", "example/web:1.2@sha256:ab"]
        doc = {"values": tricky, "map": {s or "empty": s for s in tricky}}
        back = YAML.loads(GITOPS.dump(doc))
        self.assertEqual(tricky, list(back["values"]))
        self.assertEqual(doc["map"], dict(back["map"]))

    def test_plain_quantities_stay_plain(self):
        self.assertEqual("cpu: 240m\nmemory: 1104Mi", GITOPS.dump({"cpu": "240m", "memory": "1104Mi"}))

    def test_numbers_bools_and_null_keep_their_type(self):
        back = YAML.loads(GITOPS.dump({"replicas": 2, "on": True, "gone": None, "ratio": 0.5}))
        self.assertEqual({"replicas": 2, "on": True, "gone": None, "ratio": 0.5}, dict(back))


class Patch(unittest.TestCase):
    def test_patch_has_only_changed_fields(self):
        current = deployment()
        proposed = copy.deepcopy(current)
        proposed["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["memory"] = "512Mi"
        self.assertEqual({"spec": {"template": {"spec": {"containers": [
            {"name": "main", "resources": {"requests": {"memory": "512Mi"}}}]}}}}, GITOPS.patch(current, proposed))

    def test_containers_and_env_merge_by_name(self):
        current, proposed = deployment(), deployment()
        main = proposed["spec"]["template"]["spec"]["containers"][0]
        main["env"][1]["value"] = "y"
        main["env"].append({"name": "C", "value": "3"})
        self.assertEqual({"spec": {"template": {"spec": {"containers": [{"name": "main", "env": [
            {"name": "B", "value": "y"}, {"name": "C", "value": "3"}]}]}}}}, GITOPS.patch(current, proposed))

    def test_removed_field_is_null_and_removed_item_is_deleted(self):
        current, proposed = deployment(), deployment()
        del proposed["spec"]["template"]["spec"]["containers"][0]["resources"]
        proposed["spec"]["template"]["spec"]["containers"].pop()
        self.assertEqual({"spec": {"template": {"spec": {"containers": [
            {"name": "main", "resources": None}, {"name": "side", "$patch": "delete"}]}}}}, GITOPS.patch(current, proposed))

    def test_bookkeeping_and_status_are_left_out(self):
        current, proposed = deployment(), deployment()
        proposed["metadata"]["annotations"][UPDATES.RAN] = '{"main":"sha256:1"}'
        proposed["metadata"]["annotations"][UPDATES.PREVIOUS] = "{}"
        proposed["metadata"]["resourceVersion"] = "10"
        proposed["status"] = {"replicas": 2}
        proposed["spec"]["template"]["metadata"]["annotations"]["kubectl.kubernetes.io/restartedAt"] = "now"
        self.assertEqual({}, GITOPS.patch(current, proposed))

    def test_a_setting_annotation_is_kept(self):
        current, proposed = deployment(), deployment()
        proposed["metadata"]["annotations"]["homestead.io/group"] = "Media"
        self.assertEqual({"metadata": {"annotations": {"homestead.io/group": "Media"}}}, GITOPS.patch(current, proposed))

    def test_document_heads_the_patch_with_kind_name_and_namespace(self):
        text = GITOPS.document(deployment(), {"spec": {"replicas": 2}})
        self.assertEqual("apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n  namespace: lab\nspec:\n  replicas: 2", text)
        self.assertEqual(2, YAML.loads(text)["spec"]["replicas"])

    def test_image_yaml_is_one_line_per_container(self):
        text = GITOPS.image_yaml(deployment(), {"side": "example/side:2"})
        self.assertTrue(text.endswith("spec:\n  template:\n    spec:\n      containers:\n      - name: side\n        image: example/side:2"), text)


if __name__ == "__main__":
    unittest.main()
