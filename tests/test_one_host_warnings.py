"""#841: on a one-host cluster, the warnings about app volumes say why.

A volume that asks for more copies than the one host can hold reads
degraded. The banner, the alert, MQTT and the installer's check used to say
"Longhorn is rebuilding or missing a replica", which sends a person waiting
for a rebuild that never comes. They now say the cluster has one host, and
point to Match copies to hosts in Cluster Health."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_alerts as ALERTS
import homestead_cluster as CLUSTER
import homestead_mqtt as MQTT
import homestead_network_server as NET

UNPLACEABLE = {"type": "Scheduled", "status": "False", "reason": "ReplicaSchedulingFailure", "message": "replica scheduling failed"}


def row(name="pvc-a", claim="photos", replicas=3, robustness="degraded", conditions=(UNPLACEABLE,), rebuild=None):
    return {"name": name, "pvc_name": claim, "robustness": robustness, "replicas": replicas,
            "conditions": list(conditions), "rebuild": rebuild}


def nodes(count):
    return [{"name": f"node-{i}", "status": "Ready"} for i in range(1, count + 1)]


class ReasonTests(unittest.TestCase):
    def issue(self, hosts, volume):
        issues = CLUSTER.classify_cluster_health(nodes(hosts), [], [volume])["health_issues"]
        self.assertEqual(1, len(issues))
        return issues[0]

    def test_one_host_says_why_and_points_to_the_offer(self):
        issue = self.issue(1, row())
        self.assertEqual("degraded", issue["severity"])
        self.assertTrue(issue["one_host"])
        self.assertIn("one host", issue["reason"])
        self.assertIn("3 copies", issue["reason"])
        self.assertIn("Match copies to hosts", issue["reason"])
        self.assertIn("Cluster Health", issue["reason"])

    def test_otherwise_the_reason_is_as_before(self):
        for hosts, volume in ((2, row()), (1, row(conditions=())), (1, row(rebuild={"pct": 40})),
                              (1, row(replicas=1)),
                              (1, row(conditions=({"type": "Scheduled", "status": "False", "reason": "Other"},)))):
            with self.subTest(hosts=hosts, volume=volume):
                issue = self.issue(hosts, volume)
                self.assertNotIn("one_host", issue)
                self.assertEqual("Longhorn is rebuilding or missing a replica", issue["reason"])


class AlertTests(unittest.TestCase):
    def test_the_alert_carries_the_reason_and_opens_cluster_health(self):
        issues = CLUSTER.classify_cluster_health(nodes(1), [], [row()])["health_issues"]
        fact = next(f for f in ALERTS.health_facts({"health_issues": issues}) if f.get("key"))
        self.assertIn("one host", fact["body"])
        self.assertEqual("/system/cluster?section=health", fact["href"])

    def test_other_volume_alerts_still_open_volumes(self):
        issues = CLUSTER.classify_cluster_health(nodes(2), [], [row()])["health_issues"]
        fact = next(f for f in ALERTS.health_facts({"health_issues": issues}) if f.get("key"))
        self.assertEqual("/volumes", fact["href"])


class MqttTests(unittest.TestCase):
    def setUp(self):
        self.saved = {name: getattr(NET, name, None) for name in ("cached", "kget", "get_overview", "SYS_NS")}

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(NET, name, value)

    def test_a_sensor_counts_the_volumes_short_of_hosts(self):
        self.assertIn("vol_one_host", [key for key, *_ in MQTT.CLUSTER_SENSORS])
        overview = {"nodes": [], "nodes_ready": 1, "nodes_total": 1, "volumes": 3, "vol_degraded": 2,
                    "vol_faulted": 0, "vol_one_host": 2, "cpu_pct": 1, "mem_pct": 1}
        NET.cached = lambda key, ttl, fn: overview
        NET.kget = lambda path: {"items": []}
        NET.SYS_NS = set()
        self.assertEqual(2, NET.mqtt_snapshot()["cluster"]["vol_one_host"])


class OverviewTests(unittest.TestCase):
    def test_the_overview_counts_them(self):
        import homestead_overview_server as OV
        node = {"name": "node-1", "status": "Ready", "cpu_cap": 4, "cpu_used": 1, "mem_cap_gb": 8, "mem_used_gb": 2}
        saved = {name: getattr(OV, name, None) for name in ("cached", "NETWORK")}
        try:
            OV.cached = lambda key, ttl, fn: []
            OV.NETWORK = type("N", (), {"shared_vip": staticmethod(lambda: "")})
            out = OV.get_overview(nodes=[node], wl=[], vols=[row(), row("pvc-b", claim="notes")],
                                  pods={"items": []})
        finally:
            for name, value in saved.items():
                setattr(OV, name, value)
        self.assertEqual(2, out["vol_one_host"])


if __name__ == "__main__":
    unittest.main()
