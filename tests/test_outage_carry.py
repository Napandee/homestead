"""#741: outage webhooks saved before the LAN allowance existed keep working.

Before #741 a webhook could reach a LAN address without anyone allowing it.
Now send() refuses a LAN address unless the webhook carries "lan". So the
leader, once, sets "lan" on every webhook saved in the old shape (no "format"
key) whose address resolves to a LAN address. It grants only the LAN
allowance: an address that is never reached stays refused and unmarked.

What it has done is kept in the cluster, in a ConfigMap of Homestead's own,
so a restart never runs it again; a webhook it could not carry is an alert
until an admin allows the LAN or the webhook is saved again."""
import inspect
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_names as NAMES
import homestead_outage as O
import homestead_outage_carry as C

ADDRESSES = {"ntfy.example.com": ["10.0.0.5"], "public.example.com": ["93.184.216.34"],
             "mixed.example.com": ["93.184.216.34", "10.0.0.6"], "loop.example.com": ["127.0.0.1"],
             "meta.example.com": ["169.254.169.254"], "teredo.example.com": ["2001::1"],
             "nat64.example.com": ["64:ff9b:1::a00:5"],
             "lanmeta.example.com": ["10.0.0.5", "169.254.169.254"]}
DEPLOYMENTS = "/apis/apps/v1/deployments"
VMS = "/apis/kubevirt.io/v1/virtualmachines"
STORE = f"/api/v1/namespaces/lab/configmaps/{NAMES.object_name('outage-carry')}"
COLLECTION = "/api/v1/namespaces/lab/configmaps"
T0 = 1_800_000_000
DAY = 24 * 3600


def missing(path):
    return urllib.error.HTTPError(path, 404, "missing", {}, None)


def down(path):
    return urllib.error.HTTPError(path, 503, "unavailable", {}, None)


class Cluster:
    """Deployments, VMs and Homestead's ConfigMaps, as the API server keeps them."""

    def __init__(self):
        self.objects, self.maps, self.patches = {}, {}, []
        self.failing = set()           # paths whose reads fail
        self.partial = set()           # lists that come back cut short
        self.refuse_writes = set()     # paths whose writes fail
        self.kubevirt = True

    def kget(self, path):
        if path in self.failing:
            raise down(path)
        if path == "/apis":
            return {"groups": [{"name": "apps"}] + ([{"name": "kubevirt.io"}] if self.kubevirt else [])}
        if path in (DEPLOYMENTS, VMS):
            if path == VMS and not self.kubevirt:
                raise missing(path)
            kind = "vm" if path == VMS else "app"
            return {"metadata": {"continue": "x"} if path in self.partial else {},
                    "items": [json.loads(json.dumps(o)) for (k, p), o in sorted(self.objects.items()) if k == kind]}
        for (kind, p), obj in self.objects.items():
            if p == path:
                return json.loads(json.dumps(obj))
        if path in self.maps:
            return json.loads(json.dumps(self.maps[path]))
        raise missing(path)

    def ksend(self, method, path, body=None, **options):
        if path in self.refuse_writes:
            raise down(path)
        if method == "PATCH":
            for (kind, p), obj in self.objects.items():
                if p == path:
                    sent = body["metadata"]
                    if sent.get("resourceVersion") and sent["resourceVersion"] != obj["metadata"]["resourceVersion"]:
                        raise urllib.error.HTTPError(path, 409, "conflict", {}, None)
                    obj["metadata"]["annotations"].update(sent["annotations"])
                    self.patches.append((path, body))
                    return obj
            raise missing(path)
        if method == "POST":
            path = f"{path}/{body['metadata']['name']}"
        self.maps[path] = json.loads(json.dumps(body))
        self.maps[path]["metadata"]["resourceVersion"] = str(len(self.maps) + 100)
        return body

    def add(self, name, actions, kind="app"):
        path = O._path_of(kind, "lab", name)
        self.objects[(kind, path)] = {"metadata": {"name": name, "namespace": "lab", "resourceVersion": "7",
                                                   "annotations": {NAMES.key(O.KEY): json.dumps(actions)}}}

    def actions(self, name, kind="app"):
        return json.loads(self.objects[(kind, O._path_of(kind, "lab", name))]["metadata"]["annotations"][NAMES.key(O.KEY)])

    def state(self):
        found = self.maps.get(STORE)
        return json.loads(found["data"][C.STATE_KEY]) if found else None


class CarryBase(unittest.TestCase):
    def setUp(self):
        self.cluster = Cluster()
        saved = dict(O._bound)
        self.addCleanup(lambda: (O._bound.clear(), O._bound.update(saved)))
        O.bind("/nonexistent", kget=self.cluster.kget, ksend=self.cluster.ksend,
               dns_name=lambda value, what: value, guard=lambda ns, name: None, ns="lab")
        self.looked = []
        for patch in (mock.patch.object(O, "_resolve", side_effect=self.resolve), mock.patch("builtins.print")):
            patch.start()
            self.addCleanup(patch.stop)
        self.restart()
        self.addCleanup(self.restart)

    def resolve(self, host, port):
        self.looked.append(host)
        if host not in ADDRESSES:
            raise OSError("not found")
        return ADDRESSES[host]

    def restart(self):
        """A new copy of Homestead: nothing kept in memory."""
        C._carry.clear()

    def run_at(self, now):
        return C.carry_lan(now=now)

    def titles(self):
        return {f["key"]: f["title"] for f in C.alert_facts() if "key" in f}


class CarryTests(CarryBase):
    def test_an_old_lan_webhook_is_allowed_the_lan(self):
        old = {"restart": {"after_min": 5, "max": 2}, "webhook": "http://ntfy.example.com/alerts?token=x"}
        self.cluster.add("frigate", old)
        self.cluster.add("ha", {"webhook": "http://mixed.example.com/x"}, "vm")
        result = self.run_at(T0)
        self.assertEqual(["app:lab/frigate", "vm:lab/ha"], sorted(result["marked"]))
        self.assertEqual({**old, "lan": True}, self.cluster.actions("frigate"))
        self.assertEqual({"webhook": "http://mixed.example.com/x", "lan": True}, self.cluster.actions("ha", "vm"))
        path, body = self.cluster.patches[0]
        self.assertEqual("7", body["metadata"]["resourceVersion"], "a save made meanwhile is not overwritten")
        self.assertTrue(self.cluster.state()["done"])

    def test_an_old_public_webhook_is_untouched(self):
        self.cluster.add("frigate", {"webhook": "https://public.example.com/x"})
        self.assertTrue(self.run_at(T0)["done"])
        self.assertEqual([], self.cluster.patches)
        self.assertEqual({}, self.titles())

    def test_an_address_never_reached_stays_refused(self):
        for name in ("loop", "meta", "teredo", "nat64"):
            self.cluster.add(name, {"webhook": f"http://{name}.example.com/x"})
        self.assertTrue(self.run_at(T0)["done"])
        self.assertEqual([], self.cluster.patches)
        self.assertEqual({}, self.titles())

    def test_a_lan_address_with_one_never_reached_is_not_marked_and_said(self):
        self.cluster.add("frigate", {"webhook": "http://lanmeta.example.com/x"})
        result = self.run_at(T0)
        self.assertTrue(result["done"])
        self.assertEqual([], self.cluster.patches, "the LAN allowance would not make it reachable")
        self.assertEqual({"outage-lan:app:lab/frigate": "The outage webhook for frigate needs a new address"}, self.titles())

    def test_a_webhook_saved_since_is_untouched(self):
        self.cluster.add("a", {"webhook": "http://ntfy.example.com/x", "format": "generic"})
        self.cluster.add("b", {"webhook": "http://ntfy.example.com/x", "format": "ntfy"})
        self.cluster.add("c", {"webhook": "http://public.example.com/x", "lan": True})
        self.cluster.add("d", {"restart": {"after_min": 5, "max": 2}})
        self.assertTrue(self.run_at(T0)["done"])
        self.assertEqual([], self.cluster.patches)
        self.assertEqual([], self.looked)

    def test_a_save_after_this_release_always_records_its_format(self):
        self.assertEqual({"webhook": "https://h.example.com/x", "format": "generic"},
                         O.clean({"webhook": "https://h.example.com/x"}))


class OnceTests(CarryBase):
    def test_a_restart_never_runs_it_again(self):
        self.cluster.add("frigate", {"webhook": "http://ntfy.example.com/x"})
        self.assertTrue(self.run_at(T0)["done"])
        self.cluster.add("frigate", {"webhook": "http://ntfy.example.com/x"})    # put back as before
        self.looked.clear()
        self.restart()
        self.assertTrue(self.run_at(T0 + 3600)["done"])
        self.assertEqual(1, len(self.cluster.patches))
        self.assertEqual([], self.looked, "no name is looked up again")

    def test_once_done_this_copy_does_not_read_the_cluster_again(self):
        self.run_at(T0)
        self.cluster.failing.add(STORE)
        self.assertTrue(self.run_at(T0 + 3600)["done"])

    def test_the_marker_lives_in_a_configmap_of_homesteads_own(self):
        self.run_at(T0)
        made = self.cluster.maps[STORE]
        self.assertEqual("ConfigMap", made["kind"])
        self.assertEqual("true", made["metadata"]["labels"][NAMES.key("managed")])
        self.assertEqual(T0, self.cluster.state()["done"])


class ListTests(CarryBase):
    def test_a_failing_vm_list_is_not_taken_for_no_vms(self):
        self.cluster.add("frigate", {"webhook": "http://ntfy.example.com/x"})
        self.cluster.add("ha", {"webhook": "http://ntfy.example.com/x"}, "vm")
        self.cluster.failing.add(VMS)
        self.assertFalse(self.run_at(T0)["done"])
        self.assertEqual([], self.cluster.patches, "nothing is done on half a picture")
        self.assertEqual([], self.looked)
        self.assertFalse((self.cluster.state() or {}).get("done"))
        self.cluster.failing.clear()
        self.assertIsNone(self.run_at(T0 + 10), "not before the next round")
        self.assertTrue(self.run_at(T0 + C.EVERY)["done"])
        self.assertEqual({"webhook": "http://ntfy.example.com/x", "lan": True}, self.cluster.actions("ha", "vm"))

    def test_an_incomplete_app_list_is_not_taken_for_all_apps(self):
        self.cluster.add("frigate", {"webhook": "http://ntfy.example.com/x"})
        self.cluster.partial.add(DEPLOYMENTS)
        self.assertFalse(self.run_at(T0)["done"])
        self.assertEqual([], self.cluster.patches)

    def test_no_kubevirt_is_no_vms(self):
        self.cluster.kubevirt = False
        self.cluster.add("frigate", {"webhook": "http://ntfy.example.com/x"})
        self.assertTrue(self.run_at(T0)["done"])
        self.assertEqual(1, len(self.cluster.patches))


class UnresolvedTests(CarryBase):
    def test_a_name_that_does_not_resolve_is_tried_again_each_round(self):
        self.cluster.add("frigate", {"webhook": "http://later.example.com/x"})
        result = self.run_at(T0)
        self.assertFalse(result["done"])
        self.assertEqual(["app:lab/frigate"], result["pending"])
        self.assertEqual({}, self.titles(), "no alert inside the 24 hours")
        ADDRESSES["later.example.com"] = ["10.0.0.9"]
        self.addCleanup(ADDRESSES.pop, "later.example.com")
        self.assertTrue(self.run_at(T0 + C.EVERY)["done"])
        self.assertEqual({"webhook": "http://later.example.com/x", "lan": True}, self.cluster.actions("frigate"))

    def test_after_24_hours_it_stops_and_raises_an_alert(self):
        self.cluster.add("frigate", {"webhook": "http://gone.example.com/x"})
        self.run_at(T0)
        self.assertFalse(self.run_at(T0 + DAY - C.EVERY)["done"])
        result = self.run_at(T0 + DAY)
        self.assertTrue(result["done"])
        self.assertEqual({"app:lab/frigate": "unresolved"}, result["left"])
        self.assertEqual({"outage-lan:app:lab/frigate": "The outage webhook for frigate needs Allow a LAN address"},
                         self.titles())
        self.looked.clear()
        self.run_at(T0 + DAY + C.EVERY)
        self.assertEqual([], self.looked, "it stops looking")

    def test_the_24_hours_count_from_the_first_try_across_restarts(self):
        self.cluster.add("frigate", {"webhook": "http://gone.example.com/x"})
        self.run_at(T0)
        self.restart()
        self.assertTrue(self.run_at(T0 + DAY)["done"], "the first try's time is kept in the cluster")
        self.assertEqual(T0, self.cluster.state()["first"])

    def test_a_vm_alert_names_the_vm(self):
        self.cluster.add("ha", {"webhook": "http://gone.example.com/x"}, "vm")
        self.run_at(T0)
        self.run_at(T0 + DAY)
        fact = C.alert_facts()[0]
        self.assertEqual("The outage webhook for VM ha needs Allow a LAN address", fact["title"])
        self.assertEqual("/vms?panel=monitoring&ns=lab&vm=ha", fact["href"])
        self.assertEqual("degraded", fact["severity"])


class AlertClearsTests(CarryBase):
    def setUp(self):
        super().setUp()
        self.cluster.add("frigate", {"webhook": "http://gone.example.com/x"})
        self.run_at(T0)
        self.run_at(T0 + DAY)
        self.assertEqual(1, len(self.titles()))

    def test_it_clears_once_an_admin_allows_the_lan(self):
        self.cluster.add("frigate", {"webhook": "http://gone.example.com/x", "lan": True})
        self.assertEqual({}, self.titles())

    def test_it_clears_once_the_webhook_is_saved_again(self):
        self.cluster.add("frigate", {"webhook": "https://public.example.com/x", "format": "generic"})
        self.assertEqual({}, self.titles())

    def test_it_clears_when_the_app_is_gone(self):
        self.cluster.objects.clear()
        self.assertEqual({}, self.titles())

    def test_cleared_stays_cleared_after_a_restart(self):
        self.cluster.add("frigate", {"webhook": "http://gone.example.com/x", "lan": True})
        self.titles()
        self.cluster.add("frigate", {"webhook": "http://gone.example.com/x"})    # as if never allowed
        self.restart()
        self.run_at(T0 + DAY + 60)
        self.assertEqual({}, self.titles(), "a cleared one is forgotten in the cluster too")

    def test_an_unreadable_app_keeps_its_alert(self):
        self.cluster.failing.add(O._path_of("app", "lab", "frigate"))
        self.assertEqual([{"unknown_key": "outage-lan:app:lab/frigate"}], C.alert_facts())

    def test_a_new_copy_raises_it_too(self):
        self.restart()
        self.assertEqual({"outage-lan:app:lab/frigate": "The outage webhook for frigate needs Allow a LAN address"},
                         self.titles())

    def test_an_unreadable_store_says_unknown(self):
        self.restart()
        self.cluster.failing.add(STORE)
        self.assertEqual([{"unknown_prefix": "outage-lan:"}], C.alert_facts())


class BoundTests(CarryBase):
    def test_a_write_that_keeps_failing_stops_after_24_hours_and_is_said(self):
        self.cluster.add("frigate", {"webhook": "http://ntfy.example.com/x"})
        self.cluster.refuse_writes.add(O._path_of("app", "lab", "frigate"))
        self.assertFalse(self.run_at(T0)["done"])
        self.assertFalse(self.run_at(T0 + C.EVERY)["done"], "tried again next round")
        result = self.run_at(T0 + DAY)
        self.assertEqual({"app:lab/frigate": "not saved"}, result["left"])
        self.assertEqual({"outage-lan:app:lab/frigate": "The outage webhook for frigate needs Allow a LAN address"},
                         self.titles())

    def test_an_unwritable_store_means_no_lookups_and_backs_off(self):
        self.cluster.add("frigate", {"webhook": "http://ntfy.example.com/x"})
        self.cluster.refuse_writes.add(COLLECTION)
        self.assertFalse(self.run_at(T0)["done"])
        self.assertEqual([], self.looked, "nothing is looked up when where it is kept cannot be written")
        self.assertEqual([], self.cluster.patches)
        self.assertIsNone(self.run_at(T0 + C.EVERY))
        self.assertFalse(self.run_at(T0 + 2 * C.EVERY)["done"], "tried again, less often each time")
        self.assertIsNone(self.run_at(T0 + 3 * C.EVERY))
        self.cluster.refuse_writes.clear()
        self.assertIsNone(self.run_at(T0 + 5 * C.EVERY))
        self.assertTrue(self.run_at(T0 + 6 * C.EVERY)["done"])
        wait = 0
        for _ in range(40):
            wait = C._backoff(wait)
        self.assertEqual(C.MAX_WAIT, wait)

    def test_done_but_not_kept_stops_looking_and_keeps_trying_to_keep_it(self):
        self.cluster.add("frigate", {"webhook": "http://soon.example.com/x"})
        self.assertFalse(self.run_at(T0)["done"])          # the first try, kept
        ADDRESSES["soon.example.com"] = ["10.0.0.9"]
        self.addCleanup(ADDRESSES.pop, "soon.example.com")
        self.cluster.refuse_writes.add(STORE)
        self.assertTrue(self.run_at(T0 + C.EVERY)["done"])
        self.assertFalse(self.cluster.state().get("done"))
        self.looked.clear()
        self.cluster.refuse_writes.clear()
        self.run_at(T0 + 3 * C.EVERY)
        self.assertEqual([], self.looked)
        self.assertEqual(T0 + C.EVERY, self.cluster.state()["done"])


class MalformedTests(CarryBase):
    """Whatever is stored, the carry-over never raises: a crash in it would stop outage actions."""

    def test_a_webhook_that_is_not_text_is_left_alone(self):
        for n, value in enumerate((5, ["http://ntfy.example.com/x"], {"url": "x"}, True)):
            self.cluster.add(f"odd{n}", {"webhook": value})
        self.cluster.add("frigate", {"webhook": "http://ntfy.example.com/x"})
        result = self.run_at(T0)
        self.assertTrue(result["done"])
        self.assertEqual(["app:lab/frigate"], result["marked"])
        self.assertEqual(1, len(self.cluster.patches))
        self.assertEqual({}, self.titles())

    def test_a_first_try_time_that_is_not_a_number_starts_the_day_again(self):
        self.cluster.add("frigate", {"webhook": "http://gone.example.com/x"})
        for bad in ("soon", None, [1], {"at": 1}, True):
            self.restart()
            self.cluster.maps.pop(STORE, None)
            C._write(lambda current: {"first": bad, "left": "x"})
            result = self.run_at(T0)
            self.assertFalse(result["done"], bad)
            self.assertEqual(T0, self.cluster.state()["first"], bad)

    def test_a_kept_state_that_is_not_json_starts_again(self):
        self.cluster.add("frigate", {"webhook": "http://ntfy.example.com/x"})
        self.cluster.maps[STORE] = {"apiVersion": "v1", "kind": "ConfigMap",
                                    "metadata": {"name": STORE.rsplit("/", 1)[1], "resourceVersion": "3"},
                                    "data": {C.STATE_KEY: "{not json"}}
        self.assertTrue(self.run_at(T0)["done"])
        self.assertEqual(T0, self.cluster.state()["done"])

    def test_left_entries_that_are_not_names_are_ignored(self):
        self.cluster.maps.pop(STORE, None)
        C._write(lambda current: {"first": T0, "done": T0, "left": {"nonsense": "unresolved", "app:nons": "x", "app:lab/a": 3}})
        self.assertEqual([], C.alert_facts())

    def test_names_looked_up_in_one_round_are_bounded(self):
        for n in range(C.MAX_LOOKUPS + 5):
            self.cluster.add(f"app{n:02d}", {"webhook": f"http://gone{n}.example.com/x"})
        self.run_at(T0)
        self.assertEqual(C.MAX_LOOKUPS, len(self.looked))
        self.looked.clear()
        self.run_at(T0 + C.EVERY)
        self.assertEqual(C.MAX_LOOKUPS, len(self.looked), "the rest wait for the next round")
        self.assertIn("gone24.example.com", self.looked, "and each round starts where the last stopped")

    def test_a_day_on_every_name_gets_its_answer(self):
        for n in range(C.MAX_LOOKUPS + 5):
            self.cluster.add(f"app{n:02d}", {"webhook": f"http://gone{n}.example.com/x"})
        self.run_at(T0)
        self.assertFalse(self.run_at(T0 + DAY)["done"], "names not looked up yet are still pending")
        result = self.run_at(T0 + DAY + C.EVERY)
        self.assertTrue(result["done"])
        self.assertEqual(C.MAX_LOOKUPS + 5, len(result["left"]))


class LeaderTests(unittest.TestCase):
    """The carry-over runs where outage actions run: on the leader, each monitoring round."""

    def round(self, leader, found=None):
        import server
        routes = server.OVERVIEW_ROUTES
        found = [{"ns": "lab", "name": "frigate", "outage_actions": {"webhook": "http://ntfy.example.com/x"}}] if found is None else found
        with mock.patch.object(routes.LEADER, "is_leader", return_value=leader), \
             mock.patch.object(routes.UPTIME, "observe"), mock.patch.object(routes.UPTIME, "report", return_value={}), \
             mock.patch.object(routes, "beat"), \
             mock.patch.object(routes, "cached", side_effect=lambda key, ttl, fn: found if key == "wl" else []), \
             mock.patch.object(routes.OUTAGE_CARRY, "carry_lan") as carry, mock.patch.object(routes.OUTAGE, "tick"), \
             mock.patch.object(routes.time, "sleep", side_effect=StopIteration):
            with self.assertRaises(StopIteration):
                routes._uptime_loop()
        return carry

    def test_the_leader_runs_it(self):
        self.round(True).assert_called_once()

    def test_the_leader_runs_it_with_no_actions_listed(self):
        self.round(True, found=[]).assert_called_once()

    def test_another_copy_does_not(self):
        self.round(False).assert_not_called()

    def tick(self, carry):
        import server
        routes = server.OVERVIEW_ROUTES
        found = [{"ns": "lab", "name": "frigate", "outage_actions": {"webhook": "http://ntfy.example.com/x"}}]
        calls = []
        with mock.patch.object(routes, "cached", side_effect=lambda key, ttl, fn: found if key == "wl" else []), \
             mock.patch.object(routes.UPTIME, "report", return_value={}), \
             mock.patch.object(routes.OPS, "snapshot", return_value=[]), \
             mock.patch.object(routes.OUTAGE_CARRY, "carry_lan", side_effect=lambda: (calls.append("carry"), carry())), \
             mock.patch.object(routes.OUTAGE, "tick", side_effect=lambda *a: calls.append("tick") or ["did"]), \
             mock.patch("builtins.print"):
            result = routes.outage_tick()
        return calls, result

    def test_a_carry_over_that_raises_never_stops_outage_actions(self):
        def broken():
            raise TypeError("anything at all")
        calls, result = self.tick(broken)
        self.assertEqual(["tick", "carry"], calls, "due actions first, then the carry-over")
        self.assertEqual(["did"], result)

    def test_its_alerts_are_a_source(self):
        import server
        self.assertIn('take("outage-lan", OUTAGE_CARRY.alert_facts)', inspect.getsource(server.OVERVIEW_ROUTES._alert_sources))

    def test_server_lends_it_homesteads_namespace(self):
        import server
        self.assertEqual(server.DEFAULT_NS, O._bound["ns"])


if __name__ == "__main__":
    unittest.main()
