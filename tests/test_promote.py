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

