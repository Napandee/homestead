"""Make an app host a server, in place (#364 step 5).

The host keeps its identity: k3s-agent-uninstall.sh leaves
/etc/rancher/node/password, so the server install registers as the same
Node, with its labels, Longhorn node and replicas. Removing the host and
joining it again would discard its replicas instead.

plan() checks first. start() files a job; a thread cordons and drains the
host, reads the server token from a server host, and launches a detached
systemd unit on the host that uninstalls the agent and installs a server
- it has to be detached, since the uninstall stops Homestead's own helper
pod there. The job's resolver follows the unit's status file, then waits
for the same Node to come back as a Ready etcd member."""
import json
import re
import shlex

KIND = "host-promote"
UNIT = "homestead-promote"
DIR = "/var/lib/homestead"
SCRIPT = f"{DIR}/promote.sh"
TOKEN = f"{DIR}/promote.token"
STATUS = f"{DIR}/promote.status"
JOB = f"{DIR}/promote.job"
INSTALLER = f"{DIR}/k3s-install.sh"
LOG = "/var/log/homestead-promote.log"
NODE_PASSWORD = "/etc/rancher/node/password"
TOKEN_FILE = "/var/lib/rancher/k3s/server/node-token"
# The host's own settings a server keeps; anything else it was started with
# is named in the plan, since the reinstall drops it.
NODE_VALUE_FLAGS = ("--node-name", "--node-ip", "--node-external-ip", "--flannel-iface", "--node-label", "--node-taint",
                    "--kubelet-arg")
# A Node named from a random id would come back as another Node.
RENAMING_FLAGS = ("--with-node-id",)
# What every agent is started with, and the server replaces.
EXPECTED_FLAGS = ("--server", "--token", "--token-file", "--agent-token")
TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9:._-]+$")
K3S_DOMAINS = ("kubernetes.io", "k3s.io")


def _args(node):
    try:
        args = json.loads(((node.get("metadata") or {}).get("annotations") or {}).get("k3s.io/node-args") or "[]")
    except ValueError:
        return []
    return [str(a) for a in args] if isinstance(args, list) else []


def node_flags(node):
    """(flags to keep, shell-quoted; names of the flags dropped) from the
    host's own k3s.io/node-args. Without --node-ip there, its InternalIP."""
    args, keep, dropped, i = _args(node), [], [], 0
    while i < len(args):
        flag, value = args[i], None
        i += 1
        if not flag.startswith("--"):
            continue  # "agent", the command itself
        if "=" in flag:
            flag, value = flag.split("=", 1)
        takes = flag in NODE_VALUE_FLAGS or flag in EXPECTED_FLAGS
        if value is None and takes and i < len(args):
            value, i = args[i], i + 1
        if flag in NODE_VALUE_FLAGS:
            keep += [flag, shlex.quote(value or "")]
        elif flag not in EXPECTED_FLAGS and flag not in dropped:
            dropped.append(flag)
    if "--node-ip" not in keep:
        ip = next((a.get("address") for a in (node.get("status") or {}).get("addresses") or []
                   if a.get("type") == "InternalIP"), "")
        if ip:
            keep = ["--node-ip", shlex.quote(ip)] + keep
    return keep, dropped


def own_labels(node):
    """The Node's labels that k3s and Kubernetes do not set themselves."""
    out = {}
    for key, value in ((node.get("metadata") or {}).get("labels") or {}).items():
        domain = key.split("/", 1)[0] if "/" in key else ""
        if any(domain == d or domain.endswith("." + d) for d in K3S_DOMAINS):
            continue
        out[key] = value
    return out


def promote_script(version, server, args):
    """What the unit runs on the host. The installer is fetched before
    anything is removed (exit 20: nothing changed); the node password is
    checked after the uninstall (exit 10). The token is deleted whatever
    happens. k3s is left as the installer leaves it: stopping a server that
    joined etcd never protects quorum, and with one member before it would
    lose it."""
    return "\n".join([
        "set -eu",
        f"trap 'rm -f {TOKEN}' EXIT",
        f"curl -sfL https://get.k3s.io -o {INSTALLER} || exit 20",
        "/usr/local/bin/k3s-agent-uninstall.sh",
        f"[ -s {NODE_PASSWORD} ] || {{ echo 'The node password went with the agent; not joining as a new node'; exit 10; }}",
        f"INSTALL_K3S_VERSION={shlex.quote(version)} K3S_TOKEN=\"$(cat {TOKEN})\" "
        f"sh {INSTALLER} server --server {shlex.quote(f'https://{server}:6443')}" + "".join(f" {a}" for a in args),
        "",
    ])


def check_token(token):
    """The token as k3s writes it, or ValueError - which never repeats it."""
    if not TOKEN_SHAPE.match(token or ""):
        raise ValueError("The server token read from the cluster is not in the shape k3s writes; nothing was changed")
    return token


def launch_script(op_id, length, body):
    """Run once through hostrun, the token on its stdin (never on a command
    line): write the script, then the token, root-only, and start the unit.
    One launch at a time (flock where the host has it); none if this job
    started the unit already. The token goes again if the unit does not start."""
    if not re.match(r"^[A-Za-z0-9]+$", op_id) or not isinstance(length, int) or length <= 0:
        raise ValueError("bad job id or token length")
    return "\n".join([
        "set -e",
        "umask 077",
        f"mkdir -p {DIR}",
        f"exec 9> {DIR}/promote.lock",
        "if command -v flock >/dev/null; then flock -n 9 || { echo BUSY; exit 1; }; fi",
        f'[ "$(cat {JOB} 2>/dev/null)" = {op_id} ] && {{ echo STARTED; exit 0; }}',
        f"rm -f {STATUS}",
        f"cat > {SCRIPT} <<'HSSCRIPT'", body.rstrip("\n"), "HSSCRIPT",
        f"trap 'rm -f {TOKEN}' EXIT",
        f"head -c {length} > {TOKEN}",
        f'[ "$(wc -c < {TOKEN})" -eq {length} ]',
        f"echo {op_id} > {JOB}",
        f"systemd-run --unit={UNIT} --collect --quiet sh -c 'sh {SCRIPT} > {LOG} 2>&1; echo $? > {STATUS}' "
        f"|| {{ rm -f {JOB}; exit 1; }}",
        "trap - EXIT",
        "echo STARTED",
        "",
    ])
