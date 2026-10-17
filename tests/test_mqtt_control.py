import json
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_agent_audit as AUDIT
import homestead_mqtt as MQTT

DEFAULT_ITEMS, DEFAULT_ACT = MQTT.control_items, MQTT.control_act

ITEMS = [
    {"id": "vm:vms/win11", "kind": "vm", "ns": "vms", "name": "win11", "on": True, "refused": ""},
    {"id": "app:lab/frigate", "kind": "app", "ns": "lab", "name": "frigate", "on": False, "refused": ""},
    {"id": "app:lab/homestead", "kind": "app", "ns": "lab", "name": "homestead", "on": True,
     "refused": "it is part of Homestead"},
]


class FakeClient:
    def __init__(self):
        self.sent = []

    def publish(self, topic, message, retain=True):
        self.sent.append((topic, message))


class Store:
    """The ConfigMap and Secret, kept in memory."""
    def __init__(self, stored=None):
        self.map = {"metadata": {"resourceVersion": "1"}, "data": {"mqtt.json": json.dumps(stored or {})}}

    def kget(self, path):
        if "/configmaps/" in path:
            return self.map
        raise urllib.error.HTTPError(path, 404, "not found", {}, None)

    def ksend(self, method, path, body=None, **_):
        if "/configmaps" in path and method in ("PUT", "POST"):
            self.map = {"metadata": {"resourceVersion": "2"}, "data": body["data"]}
        return {}

    def cfg(self):
        return json.loads(self.map["data"]["mqtt.json"])


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.store = Store({"enabled": True, "host": "192.0.2.177", "control": True,
                            "controlled": ["vm:vms/win11", "app:lab/frigate"]})
        MQTT.bind(self.store.kget, self.store.ksend, "default", lambda: None, lambda: True)
        self.calls = []
        MQTT.bind_control(lambda: [dict(i) for i in ITEMS], lambda *call: self.calls.append(call) or "")
        MQTT._recent.clear()
        MQTT._minute.clear()
        self.audit_dir = tempfile.TemporaryDirectory()
        AUDIT.bind(self.audit_dir.name)

    def tearDown(self):
        self.audit_dir.cleanup()

    def audited(self):
        return AUDIT._read()

    def test_commands_are_read_from_their_topics(self):
        cfg = MQTT.load()
        self.assertEqual(("vm", "vms", "win11", "start"), MQTT.parse_command(cfg, "harvester/control/vm/vms/win11/power", b"ON"))
        self.assertEqual(("app", "lab", "frigate", "stop"), MQTT.parse_command(cfg, "harvester/control/app/lab/frigate/power", b"off"))
        self.assertEqual(("vm", "vms", "win11", "force-off"), MQTT.parse_command(cfg, "harvester/control/vm/vms/win11/force_off", b"PRESS"))
        self.assertIsNone(MQTT.parse_command(cfg, "harvester/control/vm/vms/win11/state", b"ON"))
        for topic, payload in (("harvester/control/app/lab/frigate/reset", b"PRESS"),     # not for a container
                               ("harvester/control/vm/vms/win11/power", b"PRESS"),
                               ("harvester/control/vm/vms/win11/restart", b"ON"),
                               ("harvester/control/pod/lab/x/power", b"ON"),
                               ("harvester/control/vm/Bad_NS/x/power", b"ON"),
                               ("harvester/control/vm/vms/power", b"ON"),
                               ("other/control/vm/vms/win11/power", b"ON")):
            with self.subTest(topic=topic), self.assertRaises(ValueError):
                MQTT.parse_command(cfg, topic, payload)

    def test_a_vm_gets_a_switch_and_three_buttons_a_container_one(self):
        cfg = MQTT.load()
        topics = dict(MQTT.control_discovery(cfg, ITEMS[:2]))
        power = topics["homeassistant/switch/hs_vm_vms_win11/config"]
        self.assertEqual(("harvester/control/vm/vms/win11/power", "harvester/control/vm/vms/win11/state", "harvester_cluster"),
                         (power["command_topic"], power["state_topic"], power["device"]["via_device"]))
        self.assertIn("homeassistant/button/hs_vm_vms_win11_reset/config", topics)
        self.assertIn("homeassistant/button/hs_vm_vms_win11_force_off/config", topics)
        self.assertIn("homeassistant/button/hs_app_lab_frigate_restart/config", topics)
        self.assertNotIn("homeassistant/button/hs_app_lab_frigate_reset/config", topics)
        self.assertEqual(4 + 2, len(topics))
        self.assertEqual([("harvester/control/vm/vms/win11/state", "ON"), ("harvester/control/app/lab/frigate/state", "OFF")],
                         MQTT.control_states(cfg, ITEMS[:2]))

    def test_only_items_picked_and_allowed_are_covered(self):
        self.store.map["data"]["mqtt.json"] = json.dumps({**self.store.cfg(), "controlled": ["vm:vms/win11", "app:lab/homestead"]})
        self.assertEqual(["vm:vms/win11"], [i["id"] for i in MQTT.controlled(MQTT.load())])
        self.store.map["data"]["mqtt.json"] = json.dumps({**self.store.cfg(), "control": False})
        self.assertEqual([], MQTT.controlled(MQTT.load()))

    def test_saving_control_keeps_the_broker_and_retires_what_was_dropped(self):
        result = MQTT.save_control({"control": True, "controlled": ["vm:vms/win11"]})
        self.assertEqual(["vm:vms/win11"], result["controlled"])
        cfg = self.store.cfg()
        self.assertEqual(("192.0.2.177", ["app:lab/frigate"]), (cfg["host"], cfg["retire"]))
        MQTT.save_control({"control": True, "controlled": ["vm:vms/win11", "app:lab/frigate"]})
        self.assertEqual([], self.store.cfg()["retire"])   # back again: not retired
        MQTT.save_control({"control": False, "controlled": ["vm:vms/win11", "app:lab/frigate"]})
        self.assertEqual(["app:lab/frigate", "vm:vms/win11"], sorted(self.store.cfg()["retire"]))
        with self.assertRaisesRegex(ValueError, "part of Homestead"):
            MQTT.save_control({"control": True, "controlled": ["app:lab/homestead"]})
        with self.assertRaises(ValueError):
            MQTT.save_control({"control": True, "controlled": ["lab/frigate"]})

    def test_saving_the_broker_leaves_control_as_it_was(self):
        MQTT.save({"enabled": True, "host": "192.0.2.178"})
        cfg = self.store.cfg()
        self.assertEqual(("192.0.2.178", True, ["vm:vms/win11", "app:lab/frigate"]), (cfg["host"], cfg["control"], cfg["controlled"]))

    def test_a_command_is_carried_out_and_written_down(self):
        self.assertEqual("ok", MQTT.handle(MQTT.load(), "harvester/control/vm/vms/win11/reset", b"PRESS", now=1000))
        self.assertEqual([("vm", "vms", "win11", "reset")], self.calls)
        entry = self.audited()[-1]
        self.assertEqual(("mqtt", "mqtt", "mqtt_vm_reset", "ok", {"namespace": "vms", "name": "win11"}),
                         (entry["key"], entry["via"], entry["action"], entry["outcome"], entry["args"]))

    def test_commands_it_does_not_cover_are_refused(self):
        cfg = MQTT.load()
        self.assertEqual("refused", MQTT.handle(cfg, "harvester/control/app/lab/homestead/power", b"OFF", now=1000))
        self.assertEqual("refused", MQTT.handle(cfg, "harvester/control/app/lab/plex/power", b"ON", now=1000))
        self.assertEqual("refused", MQTT.handle({**cfg, "control": False}, "harvester/control/vm/vms/win11/power", b"ON", now=1000))
        self.assertEqual("refused", MQTT.handle(cfg, "harvester/control/vm/vms/win11/power", b"maybe", now=1000))
        self.assertEqual([], self.calls)
        self.assertEqual(4, len(self.audited()))

    def test_commands_are_rate_limited(self):
        cfg = MQTT.load()
        self.assertEqual("ok", MQTT.handle(cfg, "harvester/control/vm/vms/win11/power", b"ON", now=1000))
        self.assertEqual("refused", MQTT.handle(cfg, "harvester/control/vm/vms/win11/power", b"OFF", now=1005))
        self.assertEqual("ok", MQTT.handle(cfg, "harvester/control/vm/vms/win11/power", b"OFF", now=1011))
        MQTT._minute[:] = [1011] * MQTT.COMMANDS_PER_MINUTE
        self.assertEqual("refused", MQTT.handle(cfg, "harvester/control/app/lab/frigate/power", b"ON", now=1012))
        self.assertEqual(2, len(self.calls))

    def test_a_failed_command_says_why(self):
        def fails(*_):
            raise ValueError("it cannot start: not enough room")
        MQTT.bind_control(lambda: [dict(i) for i in ITEMS], fails)
        self.assertEqual("error", MQTT.handle(MQTT.load(), "harvester/control/vm/vms/win11/power", b"ON", now=1000))
        self.assertIn("not enough room", self.audited()[-1]["detail"])

    def test_a_retained_command_is_never_acted_on(self):
        while not MQTT._commands.empty():
            MQTT._commands.get_nowait()
        MQTT._on_message("harvester/control/vm/vms/win11/power", b"ON", True)
        self.assertTrue(MQTT._commands.empty())
        MQTT._on_message("harvester/control/vm/vms/win11/power", b"ON", False)
        self.assertEqual(1, MQTT._commands.qsize())
        MQTT._commands.get_nowait()

    def test_publishing_retires_announces_and_states_the_items(self):
        cfg = {**MQTT.load(), "retire": ["app:lab/plex", "vm:vms/win11"]}
        client, announced = FakeClient(), {}
        snap = {"cluster": {}, "nodes": []}
        MQTT.publish_once(cfg, client, snap, announced, MQTT.controlled(cfg))
        sent = dict(client.sent)
        self.assertEqual("", sent["homeassistant/switch/hs_app_lab_plex/config"])
        self.assertEqual("", sent["harvester/control/app/lab/plex/state"])
        # win11 is covered again, so it is announced, not retired
        self.assertTrue(sent["homeassistant/switch/hs_vm_vms_win11/config"])
        self.assertEqual("ON", sent["harvester/control/vm/vms/win11/state"])
        client.sent.clear()
        MQTT.publish_once(cfg, client, snap, announced, MQTT.controlled(cfg))
        self.assertEqual({"harvester/cluster/availability", "harvester/cluster/state",
                          "harvester/control/vm/vms/win11/state", "harvester/control/app/lab/frigate/state"},
                         {t for t, _ in client.sent})


class ApiBindingTests(unittest.TestCase):
    """Without a test's own, control reads and acts through what /api/v1 is bound to."""
    def test_items_and_commands_go_through_the_api(self):
        import homestead_api_v1 as API_V1
        import homestead_host_access as HOSTACCESS
        import homestead_workloads as WORKLOADS
        calls, roles = [], []
        saved, saved_sys = dict(API_V1.ctx), WORKLOADS.SYS_NS
        API_V1.ctx.update(
            workloads=lambda: [{"ns": "lab", "name": "frigate", "desired": 1}, {"ns": "lab", "name": "homestead", "desired": 1, "self": True},
                               {"ns": "kubevirt", "name": "virt-api", "desired": 2}],
            vms=lambda: [{"ns": "vms", "name": "win11", "running": False}],
            scale=lambda *a: calls.append(("scale",) + a) or roles.append(HOSTACCESS.role()) or {"warnings": ["tight on memory"]},
            restart=lambda *a: calls.append(("restart",) + a),
            vm_power=lambda *a: calls.append(("vm",) + a) or roles.append(HOSTACCESS.role()) or {"warnings": []})
        WORKLOADS.SYS_NS = frozenset({"kubevirt"})
        try:
            items = {i["id"]: i for i in DEFAULT_ITEMS()}
            self.assertEqual((True, "", False), (items["app:lab/frigate"]["on"], items["app:lab/frigate"]["refused"], items["vm:vms/win11"]["on"]))
            self.assertEqual("it is part of Homestead", items["app:lab/homestead"]["refused"])
            self.assertEqual("it runs in a system namespace", items["app:kubevirt/virt-api"]["refused"])
            self.assertEqual("tight on memory", DEFAULT_ACT("app", "lab", "frigate", "stop"))
            DEFAULT_ACT("vm", "vms", "win11", "force-off")
            DEFAULT_ACT("app", "lab", "frigate", "restart")
            with self.assertRaises(ValueError):
                DEFAULT_ACT("app", "lab", "frigate", "reset")
        finally:
            API_V1.ctx.clear()
            API_V1.ctx.update(saved)
            WORKLOADS.SYS_NS = saved_sys
        self.assertEqual([("scale", "lab", "frigate", 0), ("vm", "vms", "win11", "force-off"), ("restart", "lab", "frigate")], calls)
        self.assertEqual(["operator", "operator"], roles)


class ProtocolTests(unittest.TestCase):
    def test_packets_read_back(self):
        packet = MQTT.publish_packet("harvester/control/vm/vms/win11/power", "ON", retain=False)
        data = bytearray(packet)

        def recv(n):
            out = bytes(data[:n])
            del data[:n]
            return out
        first, body = MQTT.read_packet(recv)
        self.assertEqual(("harvester/control/vm/vms/win11/power", b"ON", False), MQTT.parse_publish(first, body))
        sub = MQTT.subscribe_packet(1, "harvester/control/+/+/+/+")
        self.assertEqual(0x82, sub[0])
        self.assertEqual(b"\x00", sub[-1:])

    def test_it_listens_on_the_connection_it_publishes_on(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        subscribed = []

        def broker():
            conn, _ = server.accept()
            conn.recv(1024)
            conn.sendall(b"\x20\x02\x00\x00")
            subscribed.append(conn.recv(1024))
            conn.sendall(b"\x90\x03\x00\x01\x00")      # SUBACK
            conn.sendall(MQTT.publish_packet("harvester/control/app/lab/frigate/power", "ON", retain=True))
            conn.sendall(MQTT.publish_packet("harvester/control/app/lab/frigate/power", "OFF", retain=False))
            conn.recv(1024)
            conn.close()
        thread = threading.Thread(target=broker, daemon=True)
        thread.start()
        got, done = [], threading.Event()

        def on_message(topic, payload, retained):
            got.append((topic, payload, retained))
            if len(got) == 2:
                done.set()
        client = MQTT.Client({"host": "127.0.0.1", "port": port}, "", "t").connect(timeout=5)
        client.listen("harvester/control/+/+/+/+", on_message)
        self.assertTrue(done.wait(5))
        client.close()
        thread.join(5)
        server.close()
        self.assertIn(b"harvester/control/+/+/+/+", subscribed[0])
        self.assertEqual([(b"ON", True), (b"OFF", False)], [(p, r) for _, p, r in got])
        self.assertFalse(client.lost.is_set())


if __name__ == "__main__":
    unittest.main()


class SharedStatusTests(unittest.TestCase):
    """The leader publishes; a replica on standby shows what the leader is doing."""
    def setUp(self):
        self.maps, self.writes = {}, []

        def kget(path):
            name = path.rsplit("/", 1)[-1]
            if name not in self.maps:
                raise urllib.error.HTTPError(path, 404, "not found", {}, None)
            return self.maps[name]

        def ksend(method, path, body=None, **_):
            self.writes.append(method)
            if method == "PUT" and body["metadata"]["name"] not in self.maps:
                raise urllib.error.HTTPError(path, 404, "not found", {}, None)
            self.maps[body["metadata"]["name"]] = body
            return {}
        MQTT.bind(kget, ksend, "default", lambda: None, lambda: True)
        MQTT._shared.update(at=0.0, state=None)
        self.saved = dict(MQTT.STATUS)

    def tearDown(self):
        MQTT.STATUS.clear()
        MQTT.STATUS.update(self.saved)
        MQTT._shared.update(at=0.0, state=None)

    def test_a_standby_replica_shows_the_leaders_status(self):
        MQTT.STATUS.update(state="publishing", detail="publishing to 192.0.2.177:1883 every 60s", last_publish=1000, error="")
        MQTT._share(now=1000)
        self.assertEqual(["PUT", "POST"], self.writes)
        MQTT._share(now=1030)                       # same state, inside a minute: not written again
        self.assertEqual(2, len(self.writes))
        MQTT._share(now=1061)
        self.assertEqual(3, len(self.writes))
        MQTT.STATUS.update(state="standby", detail="another Homestead replica is publishing", last_publish=0)
        shown = MQTT.shown_status(60, now=1100)
        self.assertEqual(("publishing", 1000), (shown["state"], shown["last_publish"]))
        # the leader has not written for a long while: this replica's own word
        self.assertEqual("standby", MQTT.shown_status(60, now=1061 + 600)["state"])

    def test_a_publishing_replica_shows_its_own(self):
        MQTT.STATUS.update(state="error", error="the broker refused the connection")
        self.assertEqual("error", MQTT.shown_status(60, now=5)["state"])
        self.assertEqual([], self.writes)
