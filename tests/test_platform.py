import sys
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_platform as PLATFORM


def fake(groups, kubelet, daemonsets=(), extra=None):
    extra = extra or {}

    def get(path):
        if path == "/apis":
            return {"groups": [{"name": g} for g in groups]}
        if path == "/api/v1/nodes":
            return {"items": [{"metadata": {"name": "n1", "labels": {"node-role.kubernetes.io/control-plane": "true"}},
                               "status": {"nodeInfo": {"kubeletVersion": kubelet, "architecture": "amd64"},
                                          "addresses": [{"type": "InternalIP", "address": "10.0.0.5"}]}}]}
        if path == "/apis/apps/v1/namespaces/kube-system/daemonsets":
            return {"items": [{"metadata": {"name": d}} for d in daemonsets]}
        if path in extra:
            return extra[path]
        raise urllib.error.HTTPError(path, 404, "missing", None, None)
    return get


class PlatformTests(unittest.TestCase):
    def tearDown(self):
        PLATFORM.bind(None)
        PLATFORM._cached.update(at=0.0, value=None)

    def use(self, *args, **kw):
        PLATFORM.bind(fake(*args, **kw))
        PLATFORM._cached.update(at=0.0, value=None)
        return PLATFORM.detect()

    def test_harvester_keeps_kube_vip_whatever_else_is_installed(self):
        p = self.use({"harvesterhci.io", "longhorn.io", "kubevirt.io", "helm.cattle.io", "metallb.io"}, "v1.31.4+rke2r1")
        self.assertEqual(("harvester", "kube-vip", True, True), (p["distribution"], p["load_balancer"], p["longhorn"], p["kubevirt"]))
        self.assertEqual({"kube-vip.io/loadbalancerIPs": "192.0.2.242"}, PLATFORM.vip_annotations("192.0.2.242"))

    def test_a_plain_k3s_cluster(self):
        p = self.use({"helm.cattle.io"}, "v1.31.4+k3s1", daemonsets=("svclb-traefik-abc",))
        self.assertEqual(("k3s", "servicelb", False, False, ["10.0.0.5"]),
                         (p["distribution"], p["load_balancer"], p["longhorn"], p["kubevirt"], p["control_plane"]))
        guide = PLATFORM.join_guide()
        self.assertIn("K3S_URL=https://10.0.0.5:6443", guide["agent"])
        self.assertIn('INSTALL_K3S_VERSION="v1.31.4+k3s1"', guide["agent"])
        self.assertEqual("/var/lib/rancher/k3s/server/node-token", guide["token_file"])

    def test_metallb_gets_its_own_annotations_and_pools(self):
        self.use({"metallb.io"}, "v1.30.2", extra={"/apis/metallb.io/v1beta1/ipaddresspools": {"items": [
            {"metadata": {"name": "lan"}, "spec": {"addresses": ["192.0.2.240-192.0.2.250", "10.9.0.0/28"]}}]}})
        self.assertEqual({"metallb.universe.tf/loadBalancerIPs": "192.0.2.241", "metallb.universe.tf/allow-shared-ip": "homestead"},
                         PLATFORM.vip_annotations("192.0.2.241"))
        pools = PLATFORM.metallb_pools()
        self.assertEqual([{"rangeStart": "192.0.2.240", "rangeEnd": "192.0.2.250"}, {"subnet": "10.9.0.0/28"}],
                         pools[0]["spec"]["ranges"])
        self.assertEqual({}, PLATFORM.vip_annotations(""))

    def test_rke2_with_its_servicelb_on_has_kube_vip_take_only_its_class(self):
        # Homestead's installer turns RKE2's ServiceLB on: kube-vip beside it
        # must take only its own class, or it announces the nodes' addresses.
        p = self.use({"helm.cattle.io"}, "v1.31.4+rke2r1", daemonsets=("svclb-homestead-abc", "rke2-canal"))
        self.assertTrue(p["servicelb"])
        self.assertEqual("servicelb", p["load_balancer"])

    def test_rke2_without_servicelb_has_none(self):
        p = self.use({"helm.cattle.io"}, "v1.31.4+rke2r1", daemonsets=("rke2-canal",))
        self.assertFalse(p["servicelb"])

    def test_rke2_join_uses_the_supervisor_port(self):
        self.use(set(), "v1.31.4+rke2r1")
        guide = PLATFORM.join_guide()
        self.assertIn("server: https://10.0.0.5:9345", guide["config"])

    def with_args(self, args):
        node = {"metadata": {"name": "n1", "labels": {"node-role.kubernetes.io/control-plane": "true"},
                             "annotations": {"k3s.io/node-args": args}},
                "status": {"nodeInfo": {"kubeletVersion": "v1.31.4+k3s1", "architecture": "amd64"},
                           "addresses": [{"type": "InternalIP", "address": "192.0.2.5"}],
                           "conditions": [{"type": "Ready", "status": "True"}]}}
        PLATFORM.bind(lambda path: {"items": [node]} if path == "/api/v1/nodes" else fake({"helm.cattle.io"}, "v1.31.4+k3s1")(path))
        PLATFORM._cached.update(at=0.0, value=None)
        return PLATFORM.join_guide()

    def test_a_tiebreaker_joins_as_a_server_with_its_taint_and_label(self):
        guide = self.with_args('["server","--cluster-init"]')
        self.assertIn("sh -s - server --server https://192.0.2.5:6443", guide["tiebreaker"])
        self.assertIn("--node-taint node-role/tiebreaker=true:NoSchedule --node-label node-role/tiebreaker=true", guide["tiebreaker"])
        self.assertNotIn("node-role/tiebreaker", guide["server_join"])
        self.assertIn("K3S_URL=https://192.0.2.5:6443", guide["agent"])

    def test_cluster_wide_settings_are_copied_in_both_forms(self):
        guide = self.with_args('["server","--cluster-init","--disable","traefik","--disable=servicelb",'
                               '"--default-local-storage-path=/data/local","--cluster-cidr","10.42.0.0/16",'
                               '"--secrets-encryption","--node-ip","192.0.2.5","--node-name","n1"]')
        self.assertEqual(["--disable", "traefik", "--disable", "servicelb", "--default-local-storage-path", "/data/local",
                          "--cluster-cidr", "10.42.0.0/16", "--secrets-encryption"], guide["shared_settings"])
        self.assertTrue(guide["settings_known"])
        self.assertIn("--default-local-storage-path /data/local", guide["server_join"])
        self.assertNotIn("--node-ip", guide["server_join"])
        self.assertNotIn("--node-name", guide["server_join"])

    def test_tokens_and_datastore_secrets_are_never_copied(self):
        guide = self.with_args('["server","--token","********","--agent-token=secret",'
                               '"--datastore-endpoint","postgres://u:p@db.example.com/k3s"]')
        for leaked in ("secret", "postgres", "********", "--token", "--agent-token", "--datastore-endpoint"):
            self.assertNotIn(leaked, guide["server_join"] + guide["tiebreaker"], leaked)

    def test_a_switch_a_server_turned_off_is_not_copied_as_on(self):
        guide = self.with_args('["server","--embedded-registry=false","--secrets-encryption=true","--disable-kube-proxy=0"]')
        self.assertEqual(["--secrets-encryption"], guide["shared_settings"])

    def test_a_value_with_a_space_is_quoted(self):
        guide = self.with_args('["server","--default-local-storage-path","/mnt/my data"]')
        self.assertIn("--default-local-storage-path '/mnt/my data'", guide["server_join"])

    def test_no_readable_settings_says_so(self):
        guide = self.with_args("not json")
        self.assertEqual(([], False), (guide["shared_settings"], guide["settings_known"]))
        self.assertIn("--default-local-storage-path", guide["shared_flags"])

if __name__ == "__main__":
    unittest.main()
