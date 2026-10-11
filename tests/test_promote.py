import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_promote as PROMOTE

# dash where it is installed (CI), as the installer's tests do; sh otherwise.
SH = shutil.which("dash") or shutil.which("sh")


def node(name="app-1", args=None, labels=None, ip="192.0.2.11", roles=(), uid="uid-1", ready=True):
    import json
    meta = {"name": name, "uid": uid, "labels": {**{f"node-role.kubernetes.io/{r}": "true" for r in roles}, **(labels or {})},
            "annotations": {"k3s.io/node-args": json.dumps(args)} if args is not None else {}}
    return {"metadata": meta, "status": {"addresses": [{"type": "InternalIP", "address": ip}],
                                         "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
                                         "nodeInfo": {"kubeletVersion": "v1.37.1+k3s1"}}}


class FlagTests(unittest.TestCase):
    def test_node_flags_keeps_node_scoped_flags_in_both_forms(self):
        flags, dropped = PROMOTE.node_flags(node(args=["agent", "--node-ip", "192.0.2.11", "--flannel-iface=eth1",
                                                       "--node-label", "zone=a b", "--kubelet-arg", "max-pods=150"]))
        self.assertEqual(["--node-ip", "192.0.2.11", "--flannel-iface", "eth1", "--node-label", "'zone=a b'",
                          "--kubelet-arg", "max-pods=150"], flags)
        self.assertEqual([], dropped)

    def test_node_flags_adds_internal_ip_when_none_given(self):
        flags, _ = PROMOTE.node_flags(node(args=["agent"]))
        self.assertEqual(["--node-ip", "192.0.2.11"], flags)

    def test_node_flags_names_dropped_flags(self):
        _, dropped = PROMOTE.node_flags(node(args=["agent", "--server", "https://192.0.2.5:6443", "--token", "********",
                                                   "--data-dir", "/srv/k3s", "--with-node-id"]))
        self.assertEqual(["--data-dir", "--with-node-id"], dropped)

    def test_own_labels_leaves_out_what_k3s_sets(self):
        labels = PROMOTE.own_labels(node(labels={"kubernetes.io/hostname": "app-1", "beta.kubernetes.io/arch": "amd64",
                                                 "node.kubernetes.io/instance-type": "k3s", "k3s.io/hostname": "app-1",
                                                 "homelab/zone": "a", "node.longhorn.io/create-default-disk": "true"}, roles=("worker",)))
        self.assertEqual({"homelab/zone": "a", "node.longhorn.io/create-default-disk": "true"}, labels)


class ScriptTests(unittest.TestCase):
    def body(self):
        return PROMOTE.promote_script("v1.37.1+k3s1", "192.0.2.5", ["--cluster-cidr", "10.42.0.0/16", "--node-ip", "192.0.2.11"])

    def test_promote_script_order(self):
        s = self.body()
        self.assertTrue(s.startswith("set -eu\n"))
        download = s.index("https://get.k3s.io")
        uninstall = s.index("k3s-agent-uninstall.sh")
        password = s.index(PROMOTE.NODE_PASSWORD)
        install = s.index("sh " + PROMOTE.INSTALLER + " server")
        self.assertLess(download, uninstall)
        self.assertLess(uninstall, password)
        self.assertLess(password, install)
        self.assertIn("exit 10", s)
        self.assertIn("INSTALL_K3S_VERSION=v1.37.1+k3s1 ", s)
        self.assertIn("--server https://192.0.2.5:6443 --cluster-cidr 10.42.0.0/16 --node-ip 192.0.2.11", s)
        # Quoted where it matters: a value with a space stays one argument.
        odd = PROMOTE.promote_script("v1 x", "192.0.2.5", [])
        self.assertIn("INSTALL_K3S_VERSION='v1 x' ", odd)
        self.assertIn(f'K3S_TOKEN="$(cat {PROMOTE.TOKEN})"', s)

    def test_promote_script_cleans_up_on_exit(self):
        s = self.body()
        trap = s[s.index("trap "):s.index("\n", s.index("trap "))]
        self.assertIn(f"rm -f {PROMOTE.TOKEN}", trap)
        # Stopping a server that joined etcd never protects quorum, and with
        # one member before it destroys it: the trap leaves k3s alone.
        self.assertNotIn("systemctl stop k3s", s)

    def test_download_failure_has_its_own_code(self):
        s = self.body()
        self.assertIn(f"curl -sfL https://get.k3s.io -o {PROMOTE.INSTALLER} || exit 20", s)

    def test_launch_script_writes_files_then_starts_unit(self):
        s = PROMOTE.launch_script("op1", 18, self.body())
        self.assertIn("umask 077", s)
        self.assertNotIn("HSTOKEN", s)
        # The token comes over stdin, a fixed length, written last.
        self.assertIn(f"head -c 18 > {PROMOTE.TOKEN}", s)
        self.assertLess(s.index("HSSCRIPT"), s.index("head -c 18"))
        self.assertIn("flock -n 9", s)
        self.assertIn(f"echo op1 > {PROMOTE.JOB}", s)
        self.assertLess(s.index(f"echo op1 > {PROMOTE.JOB}"), s.index("systemd-run"))
        self.assertIn(f"echo $? > {PROMOTE.STATUS}", s)
        self.assertIn(f"--unit={PROMOTE.UNIT}", s)
        self.assertTrue(s.rstrip().endswith("echo STARTED"))

    def test_launch_script_skips_when_marker_names_job(self):
        s = PROMOTE.launch_script("op1", 6, self.body())
        guard = f'[ "$(cat {PROMOTE.JOB} 2>/dev/null)" = op1 ] && {{ echo STARTED; exit 0; }}'
        self.assertIn(guard, s)
        self.assertLess(s.index("flock -n 9"), s.index(guard))
        self.assertLess(s.index(guard), s.index("head -c"))

    def test_launch_script_removes_the_token_unless_the_unit_started(self):
        s = PROMOTE.launch_script("op1", 6, self.body())
        self.assertIn(f"trap 'rm -f {PROMOTE.TOKEN}' EXIT", s)
        self.assertLess(s.index("systemd-run"), s.index("trap - EXIT"))

    def test_launch_script_refuses_bad_token(self):
        for bad in ("", "K10\nrm -rf /", "K10 x", "K10'x", "K10$(id)", "K10\nHSTOKEN\nrm -rf /"):
            with self.assertRaises(ValueError) as caught:
                PROMOTE.check_token(bad)
            self.assertNotIn("rm -rf", str(caught.exception))
        PROMOTE.check_token("K10abc::server:def")

    def test_scripts_parse_under_dash(self):
        for text in (self.body(), PROMOTE.launch_script("op1", 6, self.body())):
            with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
                f.write(text)
            result = subprocess.run([SH, "-n", f.name], capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)


from unittest import mock
import urllib.error

import homestead_capacity as CAPACITY
import homestead_lifecycle as LIFECYCLE
import homestead_maintenance as MAINTENANCE
import homestead_operations as OPS
import homestead_platform as PLATFORM


class Hostrun:
    def __init__(self, out="PASSWORD\nUNINSTALL\n"):
        self.out, self.calls, self.stdins, self.waits = out, [], [], []

    def run(self, node, script, timeout=60, wait=60, stdin=b""):
        self.calls.append((node, script))
        self.stdins.append(stdin)
        self.waits.append(wait)
        return (self.out(node, script) if callable(self.out) else self.out), ""


class Cluster:
    """A k3s cluster: server-1 and server-2 (etcd), app-1 an agent."""
    def __init__(self):
        server_args = ["server", "--cluster-cidr", "10.42.0.0/16"]
        self.nodes = [node("server-1", server_args, roles=("control-plane", "etcd", "master"), ip="192.0.2.5", uid="s1"),
                      node("server-2", server_args, roles=("control-plane", "etcd", "master"), ip="192.0.2.6", uid="s2"),
                      node("app-1", ["agent", "--node-ip", "192.0.2.11"], labels={"homelab/zone": "a"})]
        self.secrets = {"app-1.node-password.k3s"}
        self.pods = []

    def kget(self, path):
        if path == "/api/v1/nodes":
            return {"items": self.nodes, "metadata": {}}
        if path.startswith("/api/v1/nodes/"):
            name = path.rsplit("/", 1)[1]
            found = next((n for n in self.nodes if n["metadata"]["name"] == name), None)
            if found is None:
                raise urllib.error.HTTPError(path, 404, "not found", {}, None)
            return found
        if path == "/api/v1/pods":
            return {"items": self.pods, "metadata": {}}
        if path.startswith("/api/v1/namespaces/kube-system/secrets/"):
            if path.rsplit("/", 1)[1] in self.secrets:
                return {"metadata": {"name": path.rsplit("/", 1)[1]}}
            raise urllib.error.HTTPError(path, 404, "not found", {}, None)
        if "longhorn.io" in path:
            if path.endswith("/replicas"):
                return {"items": [{"spec": {"nodeID": "app-1"}}], "metadata": {}}
            return {"spec": {"allowScheduling": True}}
        raise AssertionError(path)


class PlanBase(unittest.TestCase):
    def setUp(self):
        self.cluster, self.host = Cluster(), Hostrun()
        PROMOTE.bind(self.cluster.kget, lambda *a, **k: {}, self.host, lambda: "server-1")
        self.patches = [
            mock.patch.object(PLATFORM, "detect", lambda force=False: {"distribution": "k3s", "harvester": False,
                                                                        "control_plane": ["192.0.2.5"], "version": "1.37.1+k3s1"}),
            mock.patch.object(LIFECYCLE, "quorum_report", lambda: {"members": ["server-1", "server-2"], "ready": ["server-1", "server-2"],
                                                                  "total": 2, "quorum_needs": 2, "can_lose": 0}),
            mock.patch.object(CAPACITY, "every_host", lambda: {"hosts": [{"node": "app-1", "verdict": "fits", "sentence": "fits"}]}),
            mock.patch.object(MAINTENANCE, "inventory", lambda get, pods, **k: {"local_storage": [], "blockers": []}),
            mock.patch.object(OPS, "list_operations", lambda: []),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])


class PlanTests(PlanBase):
    def test_all_clear(self):
        p = PROMOTE.plan("app-1")
        self.assertEqual([], p["blockers"])
        self.assertTrue(any("2 → 3" in w for w in p["warnings"]))
        self.assertEqual("v1.37.1+k3s1", p["facts"]["version"])
        self.assertEqual("192.0.2.5", p["facts"]["server"])
        self.assertIn("--cluster-cidr", p["facts"]["settings"])
        self.assertEqual(64, len(p["review_token"]))

    def blocked(self, needle):
        p = PROMOTE.plan("app-1")
        self.assertTrue(any(needle in b for b in p["blockers"]), p["blockers"])

    def test_blocks_other_platforms(self):
        with mock.patch.object(PLATFORM, "detect", lambda force=False: {"distribution": "rke2", "harvester": True, "control_plane": []}):
            self.blocked("k3s")

    def test_blocks_without_etcd(self):
        with mock.patch.object(LIFECYCLE, "quorum_report", lambda: {"members": [], "ready": [], "total": 0, "quorum_needs": 1, "can_lose": 0}):
            self.blocked("etcd")

    def test_blocks_a_server(self):
        self.assertTrue(any("server already" in b for b in PROMOTE.plan("server-2")["blockers"]))

    def test_blocks_a_tiebreaker(self):
        self.cluster.nodes[2]["metadata"]["labels"]["node-role/tiebreaker"] = "true"
        self.blocked("tiebreaker")

    def test_blocks_a_host_not_ready(self):
        self.cluster.nodes[2]["status"]["conditions"][0]["status"] = "False"
        self.blocked("not Ready")

    def test_blocks_homestead_on_the_host(self):
        PROMOTE.bind(self.cluster.kget, lambda *a, **k: {}, self.host, lambda: "app-1")
        self.blocked("Homestead runs on app-1")

    def test_blocks_unknown_shared_settings(self):
        for n in self.cluster.nodes[:2]:
            n["metadata"]["annotations"] = {}
        self.blocked("settings")

    def test_blocks_version_mismatch(self):
        self.cluster.nodes[2]["status"]["nodeInfo"]["kubeletVersion"] = "v1.36.0+k3s1"
        self.blocked("version")

    def test_blocks_missing_node_password_file(self):
        self.host.out = "UNINSTALL\n"
        self.blocked("node password")

    def test_blocks_missing_node_password_secret(self):
        self.cluster.secrets.clear()
        self.blocked("node-password")

    def test_blocks_missing_uninstall_script(self):
        self.host.out = "PASSWORD\n"
        self.blocked("k3s-agent-uninstall.sh")

    def test_blocks_a_member_down(self):
        with mock.patch.object(LIFECYCLE, "quorum_report", lambda: {"members": ["server-1", "server-2"], "ready": ["server-1"],
                                                                   "total": 2, "quorum_needs": 2, "can_lose": 0}):
            self.blocked("server-2")

    def test_blocks_a_running_promotion(self):
        with mock.patch.object(OPS, "list_operations", lambda: [{"kind": PROMOTE.KIND, "status": "running"}]):
            self.blocked("already")

    def test_blocks_when_apps_do_not_fit(self):
        for verdict in ("waits", "no-host", "unknown"):
            with mock.patch.object(CAPACITY, "every_host", lambda v=verdict: {"hosts": [{"node": "app-1", "verdict": v, "sentence": "S-" + v}]}):
                self.blocked("S-" + verdict)

    def test_tight_is_a_warning(self):
        with mock.patch.object(CAPACITY, "every_host", lambda: {"hosts": [{"node": "app-1", "verdict": "tight", "sentence": "TIGHT"}]}):
            p = PROMOTE.plan("app-1")
        self.assertEqual([], p["blockers"])
        self.assertIn("TIGHT", " ".join(p["warnings"]))

    def test_one_member_says_two_need_both(self):
        with mock.patch.object(LIFECYCLE, "quorum_report", lambda: {"members": ["server-1"], "ready": ["server-1"],
                                                                   "total": 1, "quorum_needs": 1, "can_lose": 0}):
            self.assertTrue(any("1 → 2" in w and "both" in w for w in PROMOTE.plan("app-1")["warnings"]))

    def test_dropped_flags_and_local_storage_warn(self):
        self.cluster.nodes[2]["metadata"]["annotations"]["k3s.io/node-args"] = '["agent", "--data-dir", "/srv/k3s"]'
        with mock.patch.object(MAINTENANCE, "inventory", lambda get, pods, **k: {"local_storage": [{"pod": "lab/db-0"}], "blockers": []}):
            warnings = " ".join(PROMOTE.plan("app-1")["warnings"])
        self.assertIn("--data-dir", warnings)
        self.assertIn("lab/db-0", warnings)

    def test_review_token_follows_the_cluster(self):
        before = PROMOTE.plan("app-1")["review_token"]
        self.assertEqual(before, PROMOTE.plan("app-1")["review_token"])
        self.cluster.nodes[0]["metadata"]["annotations"]["k3s.io/node-args"] = '["server", "--cluster-cidr", "10.52.0.0/16"]'
        self.assertNotEqual(before, PROMOTE.plan("app-1")["review_token"])

    def test_plan_never_reads_the_token(self):
        PROMOTE.plan("app-1")
        self.assertFalse(any(PROMOTE.TOKEN_FILE in script for _, script in self.host.calls))

    def test_flags_that_name_the_node_are_kept_or_block(self):
        flags, _ = PROMOTE.node_flags(node(args=["agent", "--node-name", "box-1"]))
        self.assertIn("--node-name", flags)
        self.cluster.nodes[2]["metadata"]["annotations"]["k3s.io/node-args"] = '["agent", "--with-node-id"]'
        self.assertTrue(any("--with-node-id" in b for b in PROMOTE.plan("app-1")["blockers"]))
    def test_blocks_while_a_host_job_runs(self):
        for kind in ("node-power", "host-os", "os-rollout", "cluster-shutdown"):
            with mock.patch.object(OPS, "list_operations", lambda k=kind: [{"kind": k, "status": "running"}]):
                self.assertTrue(any(kind in b for b in PROMOTE.plan("app-1")["blockers"]), kind)

    def test_route_is_admin(self):
        self.assertEqual("admin", PROMOTE.ROUTES[("GET", "/api/node/promote/plan")][0])

