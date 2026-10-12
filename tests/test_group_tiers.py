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
import homestead_route_policy as ROUTE_POLICY
import homestead_routes as ROUTER
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


class MembershipTests(unittest.TestCase):
    def setUp(self):
        self.cluster = Cluster([app("ha"), app("db", cls="x-critical"), app("web", labels=FLUX),
                                app("homestead", ns="homestead"),
                                app("frigate", group="Home", follows=True),
                                app("plex", group="Media", follows=True, cls="x-low", wrote="x-low")],
                               {"Home": "critical", "Media": "low"})
        use(self, self.cluster)

    def move(self, group, *names, **extra):
        items = [{"ns": "homestead" if n == "homestead" else "lab", "name": n} for n in names]
        return GROUPS.set_workload_groups({"group": group, "items": items, **extra})

    def test_joining_a_group_follows_it(self):
        result = self.move("Home", "ha")
        self.assertEqual({"homestead.io/group": "Home", "homestead.io/tier": "group"}, self.cluster.annotations("lab/ha"))
        self.assertEqual(1, result["following"])

    def test_an_app_with_a_class_of_its_own_joins_without_following(self):
        self.move("Home", "db")
        self.assertEqual({"homestead.io/group": "Home"}, self.cluster.annotations("lab/db"))

    def test_an_app_flux_applies_joins_without_following(self):
        self.move("Home", "web")
        self.assertEqual({"homestead.io/group": "Home"}, self.cluster.annotations("lab/web"))

    def test_homestead_itself_joins_without_following(self):
        self.move("Home", "homestead")
        self.assertEqual({"homestead.io/group": "Home"}, self.cluster.annotations("homestead/homestead"))

    def test_joining_touches_no_template(self):
        before = copy.deepcopy(self.cluster.deps["lab/ha"]["spec"])
        self.move("Home", "ha", "db")
        self.assertEqual(before, self.cluster.deps["lab/ha"]["spec"])
        self.assertTrue(all(set(body) == {"metadata"} for _, _, body, _ in self.cluster.deployment_writes()))

    def test_ungrouping_removes_the_marker_and_leaves_the_class(self):
        self.move("", "plex")
        self.assertEqual({}, self.cluster.annotations("lab/plex"))
        self.assertEqual("x-low", self.cluster.deps["lab/plex"]["spec"]["template"]["spec"]["priorityClassName"])

    def test_moving_to_another_group_keeps_the_marker(self):
        self.move("Media", "frigate")
        self.assertEqual({"homestead.io/group": "Media", "homestead.io/tier": "group"}, self.cluster.annotations("lab/frigate"))

    def test_a_group_that_ends_loses_its_tier(self):
        self.move("Media", "frigate")          # Home's only app
        self.assertEqual({"Media": "low"}, self.cluster.tiers())

    def test_a_group_that_still_has_apps_keeps_its_tier(self):
        self.move("Home", "ha")
        self.move("Media", "frigate")
        self.assertEqual({"Home": "critical", "Media": "low"}, self.cluster.tiers())

    def test_renaming_moves_the_tier_to_the_new_name(self):
        self.move("House", "frigate", renamed_from="Home")
        self.assertEqual({"House": "critical", "Media": "low"}, self.cluster.tiers())
        self.assertEqual("group", self.cluster.annotations("lab/frigate")["homestead.io/tier"])

    def test_renaming_into_a_group_with_a_tier_keeps_that_tier(self):
        self.move("Media", "frigate", renamed_from="Home")
        self.assertEqual({"Media": "low"}, self.cluster.tiers())

    def test_a_change_of_groups_still_works_when_the_tiers_cant_be_read(self):
        self.cluster.unreadable = http(403, MAP)
        result = self.move("Home", "ha")
        self.assertEqual("group", self.cluster.annotations("lab/ha")["homestead.io/tier"])
        self.assertIn("couldn't", result["detail"])

    def test_a_managed_add_on_is_refused_and_nothing_is_patched(self):
        def refuse(ns, name):
            raise ValueError("managed by Homestead")
        GROUPS.guard_managed_smb = refuse     # restored by use()
        with self.assertRaisesRegex(ValueError, "managed by Homestead"):
            self.move("Home", "ha")
        self.assertEqual([], self.cluster.sent)

    def test_names_are_checked_before_anything_is_written(self):
        with self.assertRaises(ValueError):
            self.move("Home", "ha", renamed_from="x" * 41)
        self.assertEqual([], self.cluster.sent)


class GroupTierRouteTests(unittest.TestCase):
    def setUp(self):
        self.cluster = Cluster([app("ha", group="Home", follows=True),
                                app("frigate", group="Home", follows=True, cls="x-critical", wrote="x-critical"),
                                app("db", group="Home", cls="x-low"), app("web", group="Home", labels=FLUX)], {})
        use(self, self.cluster)

    def test_setting_a_tier_writes_only_the_map(self):
        result = GROUPS.set_group_tier({"group": "Home", "tier": "critical"})
        self.assertEqual({"Home": "critical"}, self.cluster.tiers())
        self.assertEqual([], self.cluster.deployment_writes(), "nothing restarts")
        # ha and frigate follow; ha runs normal, so it needs a restart; web is Flux's, at normal.
        self.assertEqual((2, 1, 1), (result["follow"], result["restart"], result["git"]))
        self.assertEqual("critical", result["tier"])

    def test_none_removes_the_groups_tier(self):
        self.cluster.cm["data"]["group-tiers.json"] = json.dumps({"Home": "critical", "Media": "low"})
        result = GROUPS.set_group_tier({"group": "Home", "tier": "none"})
        self.assertEqual({"Media": "low"}, self.cluster.tiers())
        self.assertEqual(("", 1), (result["tier"], result["restart"]), "frigate runs critical; the default is normal")

    def test_the_first_save_makes_the_map(self):
        self.cluster.cm = None
        GROUPS.set_group_tier({"group": "Home", "tier": "low"})
        method, path, body, _ = self.cluster.sent[-1]
        self.assertEqual(("POST", f"/api/v1/namespaces/{NS}/configmaps"), (method, path))
        self.assertEqual("true", body["metadata"]["labels"]["homestead.io/managed"])
        self.assertEqual({"Home": "low"}, self.cluster.tiers())

    def test_a_group_name_is_saved_as_groups_are_named(self):
        GROUPS.set_group_tier({"group": "  Home  ", "tier": "low"})
        self.assertEqual({"Home": "low"}, self.cluster.tiers())

    def test_homesteads_group_is_refused(self):
        with self.assertRaisesRegex(ValueError, "its own tier"):
            GROUPS.set_group_tier({"group": "Homestead", "tier": "critical"})
        self.assertEqual([], self.cluster.sent)

    def test_a_group_with_no_apps_is_refused(self):
        with self.assertRaisesRegex(ValueError, "no apps"):
            GROUPS.set_group_tier({"group": "Garage", "tier": "critical"})
        self.assertEqual([], self.cluster.sent)

    def test_only_the_three_tiers_or_none(self):
        with self.assertRaises(ValueError):
            GROUPS.set_group_tier({"group": "Home", "tier": "urgent"})

    def test_a_save_that_crosses_another_is_a_409_and_merges_nothing(self):
        self.cluster.cm["data"]["group-tiers.json"] = json.dumps({"Home": "low"})
        self.cluster.race = True
        result = GROUPS.set_group_tier({"group": "Home", "tier": "critical"})
        self.assertIsInstance(result, ROUTER.Reply)
        self.assertEqual(409, result.code)
        self.assertIn("changed elsewhere", result.body["error"])
        self.assertEqual({"Home": "low"}, self.cluster.tiers())

    def test_a_map_that_isnt_json_is_replaced_by_a_save(self):
        self.cluster.cm["data"]["group-tiers.json"] = "not json"
        GROUPS.set_group_tier({"group": "Home", "tier": "critical"})
        self.assertEqual({"Home": "critical"}, self.cluster.tiers())

    def test_setting_a_groups_tier_takes_the_role_setting_an_apps_tier_does(self):
        self.assertEqual("operator", ROUTE_POLICY.role("/api/workloads/group-tier", "POST"))
        for route in (("POST", "/api/workloads/group"), ("POST", "/api/workloads/group-tier")):
            self.assertEqual("operator", GROUPS.ROUTES[route][0])
        self.assertEqual("operator", ROUTE_POLICY.role("/api/workloads/group", "POST"))
        self.assertEqual(ROUTE_POLICY.role("/api/workloads/priority", "POST"),
                         ROUTE_POLICY.role("/api/workloads/group-tier", "POST"))


if __name__ == "__main__":
    unittest.main()
