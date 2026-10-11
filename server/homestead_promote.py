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
import hashlib
import json
import os
import re
import shlex
import urllib.error

import homestead_capacity as CAPACITY
import homestead_lifecycle as LIFECYCLE
import homestead_maintenance as MAINTENANCE
import homestead_operations as OPS
import homestead_platform as PLATFORM
import homestead_roles as ROLES
import homestead_self as SELF
import homestead_hostrun as HOSTRUN

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


LONGHORN = "/apis/longhorn.io/v1beta2"
# Jobs that restart or change a host: none may run while a host is reinstalled.
HOST_JOBS = ("node-power", "host-os", "os-rollout", "cluster-shutdown", "platform-upgrade")
SERVER_ROLES = ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master", "node-role.kubernetes.io/etcd")
# One host command for the checks only the host can answer.
CHECK = (f"[ -s {NODE_PASSWORD} ] && echo PASSWORD; "
         "[ -x /usr/local/bin/k3s-agent-uninstall.sh ] && echo UNINSTALL; true")


def _own_node():
    """The node this Homestead pod runs on, as server.own_node says."""
    try:
        return kget(f"/api/v1/namespaces/{SELF.NS}/pods/{os.environ.get('HOSTNAME', '')}")["spec"]["nodeName"]
    except Exception:
        return ""


# server.py sits at its line cap, so this module wires itself: Kubernetes
# through the client hostrun is bound with, looked up at each call. bind()
# is for tests.
kget = lambda path, *a, **k: HOSTRUN.kget(path, *a, **k)  # noqa: E731
ksend = lambda *a, **k: HOSTRUN.ksend(*a, **k)  # noqa: E731
hostrun = HOSTRUN
own_node = _own_node


def bind(_kget, _ksend, _hostrun, _own_node):
    global kget, ksend, hostrun, own_node
    kget, ksend, hostrun, own_node = _kget, _ksend, _hostrun, _own_node


def _name(n):
    return (n.get("metadata") or {}).get("name", "")


def _server(n):
    labels = (n.get("metadata") or {}).get("labels") or {}
    return any(k in labels for k in SERVER_ROLES)


def _ready(n):
    return any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in (n.get("status") or {}).get("conditions") or [])


def _version(n):
    return ((n.get("status") or {}).get("nodeInfo") or {}).get("kubeletVersion", "")


def _exists(path):
    try:
        kget(path)
        return True
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False
        raise


def _replicas_on(node):
    try:
        rows = kget(f"{LONGHORN}/replicas").get("items", [])
    except Exception:
        return None
    return sum(1 for r in rows if (r.get("spec") or {}).get("nodeID") == node)


def _review(node, facts, blockers):
    """The plan as a person approved it: what start() must find again."""
    canon = json.dumps({"node": node, **{k: facts[k] for k in ("uid", "version", "server", "settings", "host_settings", "members")},
                        "blockers": blockers}, sort_keys=True)
    return hashlib.sha256(canon.encode()).hexdigest()


def plan(node):
    """What making node a server would do, and what stops it."""
    nodes = kget("/api/v1/nodes").get("items", [])
    host = next((n for n in nodes if _name(n) == node), None)
    if host is None:
        raise ValueError(f"{node} is not a host in this cluster")
    p = PLATFORM.detect(True) or {}
    q = LIFECYCLE.quorum_report()
    members = list(q.get("members") or [])
    blockers, warnings = [], []
    if p.get("harvester") or p.get("distribution") != "k3s":
        blockers.append("Only a k3s app host can be made a server here")
    elif not members:
        blockers.append("This cluster keeps its state outside etcd: a server would add no vote")
    if _server(host):
        blockers.append(f"{node} is a server already")
    elif ROLES.is_tiebreaker(host):
        blockers.append(f"{node} is marked as a tiebreaker; app hosts only")
    if not _ready(host):
        blockers.append(f"{node} is not Ready")
    if own_node() == node:
        blockers.append(f"Homestead runs on {node}: move it to another host first (Host actions › Move all off), then review again")
    shared, known = PLATFORM.shared_server_args(nodes)
    if not known:
        blockers.append("The servers' shared settings could not be read from k3s.io/node-args")
    version = _version(host)
    versions = sorted({_version(n) for n in nodes if _server(n)})
    if versions != [version]:
        blockers.append(f"{node} runs k3s {version or 'unknown'}, the servers {', '.join(versions) or 'unknown'}: "
                        "upgrade so the versions match first")
    down = [m for m in members if m not in (q.get("ready") or [])]
    if down:
        blockers.append(f"etcd member{'s' if len(down) != 1 else ''} {', '.join(down)} not Ready: "
                        "the cluster takes no new member while short of a vote")
    jobs = [o for o in OPS.list_operations() if o.get("status") not in OPS.TERMINAL]
    if any(o.get("kind") == KIND for o in jobs):
        blockers.append("A host is being made a server already")
    for kind in sorted({o.get("kind") for o in jobs if o.get("kind") in HOST_JOBS}):
        blockers.append(f"A {kind} job is running: a restart or update during the reinstall could cost a vote. "
                        "Wait for it to finish")
    forecast = next((h for h in CAPACITY.every_host().get("hosts", []) if h.get("node") == node), None)
    if forecast is None or forecast.get("verdict") in ("waits", "no-host", "unknown"):
        blockers.append((forecast or {}).get("sentence") or f"Could not work out where {node}'s apps would go")
    elif forecast.get("verdict") == "tight":
        warnings.append(forecast.get("sentence", ""))
    if not _exists(f"/api/v1/namespaces/kube-system/secrets/{node}.node-password.k3s"):
        blockers.append(f"kube-system/{node}.node-password.k3s is missing: the host would not register as the same Node")
    if _ready(host):
        out, _ = hostrun.run(node, CHECK, timeout=45, wait=30)
        if "PASSWORD" not in out:
            blockers.append(f"{node} has no node password at {NODE_PASSWORD}: it would register as a new Node")
        if "UNINSTALL" not in out:
            blockers.append(f"{node} has no /usr/local/bin/k3s-agent-uninstall.sh: was k3s installed another way?")
    host_settings, dropped = node_flags(host)
    for flag in (f for f in dropped if f in RENAMING_FLAGS):
        blockers.append(f"{node} was started with {flag}: as a server it would register under another name")
    if dropped:
        warnings.append(f"{node} was started with {', '.join(dropped)}, which a server here would not keep")
    after = len(members) + 1
    warnings.append(f"{len(members)} → {after} members: " +
                    ("two members need both: add a third soon" if after == 2 else
                     "losing any one keeps the cluster up" if after % 2 else "an even count adds no failure margin"))
    pods = [x for x in kget("/api/v1/pods").get("items", []) if (x.get("spec") or {}).get("nodeName") == node]
    local = MAINTENANCE.inventory(kget, pods).get("local_storage") or []
    if local:
        warnings.append("Kept on this host, waiting for it: " + ", ".join(str(r.get("pod") or r) for r in local[:6]))
    warnings.append(f"{node} is out of service for several minutes while k3s reinstalls")
    facts = {"uid": (host.get("metadata") or {}).get("uid", ""), "version": version,
             "server": (p.get("control_plane") or [""])[0], "settings": shared, "host_settings": host_settings,
             "members": members, "replicas": _replicas_on(node)}
    return {"node": node, "blockers": blockers, "warnings": warnings, "facts": facts,
            "review_token": _review(node, facts, blockers)}


def _node_param(request):
    node = (request.query.get("node") or [""])[0]
    if not node:
        raise ValueError("which host?")
    return node


ROUTES = {
    ("GET", "/api/node/promote/plan"): ("admin", lambda request: plan(_node_param(request))),
}
