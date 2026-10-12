"""#430: a tier for a group of apps, and where each app's tier comes from."""
import copy
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_group_tiers as GROUPS
import homestead_priority as P
import homestead_updates as UPDATES
# Binds every module as the app does, before any test runs, so use() always
# puts the app's own bindings back - never a module's unbound defaults.
import server  # noqa: F401

NS = "homestead"
MAP = f"/api/v1/namespaces/{NS}/configmaps/homestead-group-tiers"
FLUX = {"kustomize.toolkit.fluxcd.io/name": "apps", "kustomize.toolkit.fluxcd.io/namespace": "flux-system"}
HELM = {"helm.toolkit.fluxcd.io/name": "ha", "helm.toolkit.fluxcd.io/namespace": "flux-system"}
ARGO = {"argocd.argoproj.io/tracking-id": "home:apps/Deployment:lab/db"}


def http(code, path="x"):
    return urllib.error.HTTPError(path, code, "refused", {}, io.BytesIO())


def pc(name, value, default=False, never=False):
    c = {"metadata": {"name": name}, "value": value}
    if default:
        c["globalDefault"] = True
    if never:
        c["preemptionPolicy"] = "Never"
    return c


CLASSES = [pc("x-critical", 100000), pc("x-normal", 10000, default=True), pc("x-low", 1000, never=True)]
LAD = P.ladder(CLASSES)


def app(name, ns="lab", group="", cls="", follows=False, wrote="", labels=None, ann=None):
    annotations = dict(ann or {})
    if group:
        annotations["homestead.io/group"] = group
    if follows:
        annotations["homestead.io/tier"] = "group"
    if wrote:
        annotations["homestead.io/tier-class"] = wrote
    spec = {"containers": [{"name": "app", "image": "example.com/app:1"}]}
    if cls:
        spec["priorityClassName"] = cls
    return {"metadata": {"name": name, "namespace": ns, "annotations": annotations, "labels": labels or {}, "generation": 1},
            "spec": {"replicas": 1, "template": {"spec": spec}},
            "status": {"observedGeneration": 1, "replicas": 1, "updatedReplicas": 1, "readyReplicas": 1}}


class Cluster:
    """Deployments, VMs, priority classes and the group-tiers ConfigMap,
    answered and changed as the API server does: a merge patch answers with
    the object, and a PUT over a stale resourceVersion is a 409."""

    def __init__(self, deps, tiers=None, raw=None):
        self.deps = {f"{d['metadata']['namespace']}/{d['metadata']['name']}": d for d in deps}
        self.vms, self.sent, self.unreadable, self.race, self.other = {}, [], None, False, None
        self.cm = None
        if tiers is not None or raw is not None:
            self.cm = {"metadata": {"name": "homestead-group-tiers", "namespace": NS, "resourceVersion": "1"},
                       "data": {"group-tiers.json": raw if raw is not None else json.dumps(tiers)}}

    def kget(self, path, **_):
        if path == "/apis/scheduling.k8s.io/v1/priorityclasses":
            return {"items": copy.deepcopy(CLASSES)}
        if path == "/apis/apps/v1/deployments":
            return {"items": [copy.deepcopy(d) for d in self.deps.values()]}
        if path == P.VMS:
            return {"items": [copy.deepcopy(v) for v in self.vms.values()]}
        if path == MAP:
            if self.unreadable:
                raise self.unreadable
            if self.cm is None:
                raise http(404, path)
            return copy.deepcopy(self.cm)
        if "/deployments/" in path:
            ns, name = path.split("/namespaces/")[1].split("/deployments/")
            if f"{ns}/{name}" not in self.deps:
                raise http(404, path)
            return copy.deepcopy(self.deps[f"{ns}/{name}"])
        if self.other is not None:
            return copy.deepcopy(self.other)
        raise http(404, path)

    def ksend(self, method, path, body=None, ctype="application/json", **_):
        self.sent.append((method, path, copy.deepcopy(body), ctype))
        if "/deployments/" in path and method == "PATCH":
            ns, name = path.split("/namespaces/")[1].split("/deployments/")
            dep = self.deps[f"{ns}/{name}"]
            annotations = dep["metadata"].setdefault("annotations", {})
            for key, value in ((body.get("metadata") or {}).get("annotations") or {}).items():
                if value is None:
                    annotations.pop(key, None)
                else:
                    annotations[key] = value
            for key, value in (((body.get("spec") or {}).get("template") or {}).get("spec") or {}).items():
                pod = dep["spec"]["template"]["spec"]
                if value is None:
                    pod.pop(key, None)
                else:
                    pod[key] = value
            return copy.deepcopy(dep)
        if method == "POST" and path == f"/api/v1/namespaces/{NS}/configmaps":
            if self.cm is not None:
                raise http(409, path)
            self.cm = copy.deepcopy(body)
            self.cm["metadata"]["resourceVersion"] = "1"
            return copy.deepcopy(self.cm)
        if method == "PUT" and path == MAP:
            if self.race or body["metadata"].get("resourceVersion") != self.cm["metadata"]["resourceVersion"]:
                raise http(409, path)
            version = str(int(self.cm["metadata"]["resourceVersion"]) + 1)
            self.cm = copy.deepcopy(body)
            self.cm["metadata"]["resourceVersion"] = version
            return copy.deepcopy(self.cm)
        raise AssertionError(f"unexpected {method} {path}")

    def tiers(self):
        return None if self.cm is None else json.loads(self.cm["data"]["group-tiers.json"])

    def annotations(self, key):
        return self.deps[key]["metadata"].get("annotations") or {}

    def deployment_writes(self):
        return [s for s in self.sent if "/deployments/" in s[1]]


def use(case, cluster):
    """Binds a fake cluster into homestead_group_tiers and homestead_priority,
    and puts both modules' real bindings back after the test: other modules'
    tests read tiers through them."""
    saved_groups = {name: getattr(GROUPS, name) for name in GROUPS.NEEDS + ("NAMESPACE", "OWN_GROUP", "managed_by")}
    saved_priority = (P.kget, P.ksend, P.managed_by, P.own, P.system_ns)

    def restore():
        for name, value in saved_groups.items():
            setattr(GROUPS, name, value)
        P.kget, P.ksend, P.managed_by, P.own, P.system_ns = saved_priority
        P._LADDER.update(at=0, value=None)
    case.addCleanup(restore)
    GROUPS.bind({"kget": cluster.kget, "ksend": cluster.ksend, "guard_managed_smb": lambda ns, name: None,
                 "_dns_name": lambda value, label="name": value,
                 "is_self": lambda ns, name: (ns, name) == ("homestead", "homestead")},
                namespace=NS, own_group="Homestead", managed=UPDATES.managed_by)
    P.bind(cluster.kget, cluster.ksend, UPDATES.managed_by, ("homestead", "homestead"), {"kube-system"}, lambda: True)
    P._LADDER.update(at=0, value=None)


class MapTests(unittest.TestCase):
    def test_no_map_yet_is_no_group_tiers(self):
        use(self, Cluster([]))
        self.assertEqual({}, GROUPS.stored())
        self.assertEqual(({}, None), GROUPS.read())

    def test_the_map_is_read_from_homesteads_namespace(self):
        use(self, Cluster([], {"Home": "critical"}))
        self.assertEqual(({"Home": "critical"}, "1"), GROUPS.read())

    def test_entries_that_arent_tiers_are_dropped(self):
        use(self, Cluster([], raw='{"Home": "critical", "Media": "urgent", "Lab": 3}'))
        self.assertEqual({"Home": "critical"}, GROUPS.stored())

    def test_a_map_that_cant_be_read_is_unknown_not_empty(self):
        cluster = Cluster([], {"Home": "critical"})
        cluster.unreadable = http(403, MAP)
        use(self, cluster)
        self.assertIsNone(GROUPS.stored())

    def test_a_map_that_isnt_a_map_of_tiers_is_unknown(self):
        for raw in ("not json", '["Home"]'):
            with self.subTest(raw=raw):
                use(self, Cluster([], raw=raw))
                self.assertIsNone(GROUPS.stored())
                self.assertEqual((None, "1"), GROUPS.read())

    def test_stored_reads_through_the_reader_it_is_given(self):
        # The plan and Edit read through their own module's kget.
        use(self, Cluster([], {"Home": "low"}))
        other = Cluster([], {"Home": "critical"})
        self.assertEqual({"Home": "critical"}, GROUPS.stored(other.kget))


class ResolveTests(unittest.TestCase):
    def test_no_marker_is_a_tier_of_its_own_even_in_a_group_with_one(self):
        self.assertEqual({"tier": "low", "class": "x-low", "source": "own", "runs": "low"},
                         GROUPS.resolve(app("db", group="Home", cls="x-low"), {"Home": "critical"}, LAD))

    def test_no_class_and_no_marker_is_normal_of_its_own(self):
        self.assertEqual({"tier": "normal", "class": None, "source": "own", "runs": "normal"},
                         GROUPS.resolve(app("web"), {}, LAD))

    def test_the_marker_follows_the_groups_tier(self):
        self.assertEqual({"tier": "critical", "class": "x-critical", "source": "group", "runs": "normal"},
                         GROUPS.resolve(app("ha", group="Home", follows=True), {"Home": "critical"}, LAD))

    def test_the_marker_in_a_group_with_no_tier_is_the_default(self):
        self.assertEqual({"tier": "normal", "class": None, "source": "default", "runs": "critical"},
                         GROUPS.resolve(app("ha", group="Home", follows=True, cls="x-critical"), {"Media": "low"}, LAD))

    def test_the_marker_with_no_group_is_the_default(self):
        self.assertEqual("default", GROUPS.resolve(app("ha", follows=True), {"Home": "critical"}, LAD)["source"])

    def test_a_class_alone_never_means_from_the_group(self):
        # x-critical is what Home's tier gives; without the marker it is still the app's own.
        self.assertEqual("own", GROUPS.resolve(app("ha", group="Home", cls="x-critical"), {"Home": "critical"}, LAD)["source"])

    def test_an_unreadable_map_leaves_a_follower_unknown_and_an_own_tier_known(self):
        self.assertEqual({"tier": None, "class": None, "source": "group", "runs": "normal"},
                         GROUPS.resolve(app("ha", group="Home", follows=True), None, LAD))
        self.assertEqual("low", GROUPS.resolve(app("db", group="Home", cls="x-low"), None, LAD)["tier"])
        self.assertEqual("default", GROUPS.resolve(app("ha", follows=True), None, LAD)["source"],
                         "no group: the map isn't needed")

    def test_vms_are_always_their_own(self):
        vm = app("win", group="Home", follows=True)   # annotations a person put on a VM by hand
        self.assertEqual("own", GROUPS.resolve(vm, {"Home": "critical"}, LAD, kind="vm")["source"])

    def test_an_app_flux_applies_is_compared_with_its_groups_tier(self):
        dep = app("web", group="Home", labels=FLUX)
        self.assertEqual({"tier": "critical", "class": "x-critical", "source": "group", "runs": "normal"},
                         GROUPS.resolve(dep, {"Home": "critical"}, LAD, managed=True))
        self.assertEqual("own", GROUPS.resolve(dep, {}, LAD, managed=True)["source"])

    def test_a_marker_on_an_app_flux_applies_is_not_followed(self):
        dep = app("web", group="Home", follows=True, cls="x-low", labels=FLUX)
        self.assertEqual({"tier": "low", "class": "x-low", "source": "own", "runs": "low"},
                         GROUPS.resolve(dep, {}, LAD, managed=True))


if __name__ == "__main__":
    unittest.main()
