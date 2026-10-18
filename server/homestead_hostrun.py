"""One checked script on a node's host, and its output.

Some fixes can only be made on the machine itself - a file k3s reads at
start, a disk to format and mount. Kubernetes reaches a node only through a
pod, so this is the node shell's helper (homestead_nodeshell) in miniature:
a privileged Alpine pod pinned to the node, sharing the host's namespaces,
that runs one script through nsenter into PID 1 - the host as SSH gives it -
and is deleted when the script ends. Only code in Homestead calls it, with
scripts written here, never text from a request.
"""
import secrets
import time
import urllib.error
import urllib.parse

import homestead_names as NAMES

kget = ksend = None
exec_in = None           # (namespace, pod, argv, timeout=, container=) -> (stdout bytes, stderr text)
NS = "lab"
IMAGE = "alpine:3.24"
# nsenter leaves the script in this pod's cgroup, so what it runs on the host -
# apt-get refreshing its lists, mkfs, update-initramfs - shares this limit. At
# 64Mi the kernel killed apt-get part-way through reading a host's OS.
MEMORY_LIMIT = "1Gi"
TASK = "host-run"
HOST = ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--", "sh", "-c"]
# A refused exec (an error with retry set) is tried this many times, this far
# apart: a minute and a half for the API server to reach a host just back. On
# RKE2 it reaches a kubelet through the host's agent tunnel, which reconnects
# well after the host reports Ready; half a minute was not enough (e2e, bonds).
EXEC_TRIES = 18
EXEC_PAUSE = 5


def bind(_kget, _ksend, _exec_in, namespace):
    global kget, ksend, exec_in, NS
    kget, ksend, exec_in, NS = _kget, _ksend, _exec_in, namespace


def pod_name(node):
    """A helper's name: the node it runs on, and a part of its own, so two
    runs on one host never share (or delete) one helper (#597)."""
    safe = "".join(c if c.isalnum() or c == "-" else "-" for c in node.lower()).strip("-")
    return f"homestead-host-{safe}"[:56].rstrip("-") + "-" + secrets.token_hex(3)


def _get(path):
    try:
        return kget(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def body(node, lifetime=900, name=""):
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name or pod_name(node), "namespace": NS,
                         "labels": {**NAMES.labels(TASK), NAMES.key("node"): node[:63]}},
            "spec": {"nodeName": node, "hostPID": True, "hostIPC": True, "hostNetwork": True,
                     "restartPolicy": "Never", "terminationGracePeriodSeconds": 1,
                     "activeDeadlineSeconds": lifetime, "tolerations": [{"operator": "Exists"}],
                     "containers": [{"name": "host", "image": IMAGE,
                                     "command": ["sh", "-c", f"trap 'exit 0' TERM; sleep {lifetime} & wait"],
                                     "securityContext": {"privileged": True},
                                     "resources": {"requests": {"cpu": "5m", "memory": "8Mi"},
                                                   "limits": {"memory": MEMORY_LIMIT}}}]}}


def _delete(path):
    try:
        ksend("DELETE", path, {"apiVersion": "v1", "kind": "DeleteOptions", "gracePeriodSeconds": 0})
    except urllib.error.HTTPError:
        pass


def _clear_ended():
    """Helpers of runs that ended without removing theirs (the run's process
    stopped, or the pod reached its deadline) are deleted - never one still
    running, which is another run's. Housekeeping: a failed read skips it."""
    selector = urllib.parse.quote(NAMES.key("task") + "=" + TASK)
    try:
        pods = (kget(f"/api/v1/namespaces/{NS}/pods?labelSelector={selector}") or {}).get("items") or []
    except Exception:
        return
    for pod in pods:
        meta = pod.get("metadata") or {}
        if ((pod.get("status") or {}).get("phase") in ("Succeeded", "Failed") and meta.get("name", "").startswith("homestead-host-")
                and NAMES.label_of(meta, "task") == TASK):
            try:
                ksend("DELETE", f"/api/v1/namespaces/{NS}/pods/{meta['name']}",
                      {"apiVersion": "v1", "kind": "DeleteOptions", "gracePeriodSeconds": 0,
                       **({"preconditions": {"uid": meta["uid"]}} if meta.get("uid") else {})})
            except urllib.error.HTTPError:
                pass


def run(node, script, timeout=120, wait=60, sleep=time.sleep, stdin=b""):
    """Run script as root on node's host; (stdout text, stderr text). The
    helper is made for this run and removed after it; another run on the
    same host has its own, which this one never touches (#597). stdin, when
    given, is what the script reads: a secret goes there, never on its
    command line."""
    _clear_ended()
    name = pod_name(node)
    path = f"/api/v1/namespaces/{NS}/pods/{name}"
    ksend("POST", f"/api/v1/namespaces/{NS}/pods", body(node, lifetime=max(300, timeout + wait + 60), name=name))
    try:
        for _ in range(int(wait * 2)):
            status = (_get(path) or {}).get("status") or {}
            if status.get("phase") == "Running":
                break
            waiting = next((((c.get("state") or {}).get("waiting") or {}) for c in status.get("containerStatuses") or []
                            if (c.get("state") or {}).get("waiting")), {})
            if waiting.get("reason") in ("ErrImagePull", "ImagePullBackOff"):
                raise ValueError(f"{node} cannot pull {IMAGE}: {waiting.get('message', '')[:200]}")
            sleep(0.5)
        else:
            raise ValueError(f"the host helper on {node} did not start within {wait} seconds")
        # A host just back can show its helper Running before the API server
        # reaches its kubelet: the exec is refused and nothing has run, so it
        # is tried again for a while (seen on RKE2 after a host failed).
        for attempt in range(EXEC_TRIES):
            try:
                out, err = exec_in(NS, name, HOST + [script], timeout=timeout, container="host",
                                   **({"stdin": stdin} if stdin else {}))
                return out.decode("utf-8", "replace"), err
            except ConnectionError as error:
                if not getattr(error, "retry", False) or attempt == EXEC_TRIES - 1:
                    raise
                sleep(EXEC_PAUSE)
    finally:
        _delete(path)
