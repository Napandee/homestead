"""The MQTT settings are changed on what is stored when they are saved, so a
change another copy made meanwhile stays (#621)."""
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
import homestead_mqtt as MQTT
from fakes.kube import Kube

MAP = f"/api/v1/namespaces/default/configmaps/{MQTT._map()}"
BROKER = {"enabled": True, "host": "192.0.2.177"}


def settings(value=None, text=None):
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": MQTT._map(), "namespace": "default"},
            "data": {"mqtt.json": text if text is not None else json.dumps(value)}}


class MqttWriteTests(unittest.TestCase):
    def setUp(self):
        saved = MQTT.kget, MQTT.ksend, MQTT.NAMESPACE, MQTT.snapshot, MQTT.is_leader
        self.addCleanup(lambda: MQTT.bind(*saved))
        items = MQTT.control_items, MQTT.control_act
        self.addCleanup(lambda: MQTT.bind_control(*items))
        MQTT.bind_control(lambda: [{"id": "vm:vms/win11", "kind": "vm", "ns": "vms", "name": "win11", "on": True, "refused": ""}],
                          lambda *call: "")

    def bind(self, send=None):
        MQTT.bind(K.kget, send or K.send, "default", lambda: None, lambda: True)

    def stored(self, kube):
        return json.loads(kube.object(MAP)["data"]["mqtt.json"])

    def test_power_control_turned_on_meanwhile_stays_when_the_broker_is_saved(self):
        with Kube(settings({**BROKER, "control": False, "controlled": []})) as kube:
            raced = []

            def send(method, path, body=None, **options):
                if method == "PUT" and path == MAP and not raced:
                    raced.append(1)
                    other = kube.object(MAP)
                    value = {**json.loads(other["data"]["mqtt.json"]), "control": True, "controlled": ["vm:vms/win11"]}
                    K.send("PUT", MAP, dict(other, data={"mqtt.json": json.dumps(value)}))
                return K.send(method, path, body, **options)
            self.bind(send)
            MQTT.save({**BROKER, "port": 1884})
            value = self.stored(kube)
        self.assertEqual(1884, value["port"])
        self.assertEqual((True, ["vm:vms/win11"]), (value["control"], value["controlled"]), "the control change was lost")

    def test_settings_that_cannot_be_read_are_not_saved_over(self):
        for save in (lambda: MQTT.save(BROKER), lambda: MQTT.save_control({"control": True, "controlled": ["vm:vms/win11"]})):
            with self.subTest(save=save), Kube(settings(text="{broken")) as kube:
                self.bind()
                with self.assertRaises(ValueError):
                    save()
                self.assertEqual("{broken", kube.object(MAP)["data"]["mqtt.json"])

    def test_a_password_is_not_changed_when_the_settings_cannot_be_saved(self):
        secret = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                  "metadata": {"name": MQTT._secret(), "namespace": "default"}, "data": {"password": "b2xk"}}
        path = f"/api/v1/namespaces/default/secrets/{MQTT._secret()}"
        for change in ({"password": "new-one"}, {"clear_password": True}):
            with self.subTest(change=change), Kube(settings(text="{broken"), secret) as kube:
                self.bind()
                with self.assertRaises(ValueError):
                    MQTT.save({**BROKER, **change})
                self.assertEqual("b2xk", kube.object(path)["data"]["password"], "the password changed although nothing was saved")


if __name__ == "__main__":
    unittest.main()
