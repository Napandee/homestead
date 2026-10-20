"""
Import pipeline + VM creation + image cache + scheduled jobs.

Import sources are remote hosts (Unraid, Proxmox, plain SSH) registered in a
ConfigMap, with credentials in a Secret. Importing a container means:
  1. create a PVC for its appdata,
  2. run a Job that rsyncs the remote path into that PVC,
  3. create the Deployment pointing at it.
Step 2 is the part that takes real time, so it runs as a Job we can poll.
"""
import base64
import copy
import json
import hashlib
import os
import re
import shlex
import secrets
import time
import homestead_copy_checks as COPY_CHECKS
import homestead_self as SELF
import urllib.error
import urllib.parse

import homestead_names as NAMES
import homestead_shared as SHARED
import homestead_hvimage as HVIMAGE
import homestead_runtime as RUNTIME
import homestead_pod_resources as RESOURCES
import homestead_import_job as IMPORT_JOB
import homestead_operations as OPS
import homestead_source_ssh as SOURCE_SSH
import homestead_capacity_review as SOURCE_REVIEW
import homestead_passthrough as PASSTHROUGH
import homestead_vm_network as VMNETWORK
import homestead_routes

kget = ksend = create_pvc = build_deployment = None
NS = "lab"
# An ISO volume a new VM can install from (homestead_isos.ready_volume), set
# by the server; None where ISOs are not offered.
iso_ready = None
# Where the image cache keeps each node's full image list
# (homestead_image_cache), set by the server.
SCAN_DIR = ""        # set by the server; empty keeps scans in memory only
_cache = {}
hardware_features = lambda: []


def bind(_kget, _ksend, _create_pvc, _build_dep, _ns, _cache_ref, _hardware_features=None):
    global kget, ksend, create_pvc, build_deployment, NS, _cache, hardware_features
    kget, ksend, create_pvc, build_deployment = _kget, _ksend, _create_pvc, _build_dep
    NS, _cache = _ns, _cache_ref
    # Name lookups need the same client, and binding here means a caller never
    # has to remember to do it separately.
    NAMES.bind(_kget)
    if _hardware_features:
        hardware_features = _hardware_features


def _bust(*keys):
    for k in list(_cache):
        if not keys or any(k.startswith(x) for x in keys):
            _cache.pop(k, None)


SAFE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$")


# --------------------------------------------------------------- sources
def _sources_map():
    return NAMES.object_name("sources", NS)


SOURCE_LOCK = SHARED.SharedLock("import-sources", strict=True, directory=lambda: OPS.DATA_DIR)


def _sources_state():
    try:
        cm = kget(f"/api/v1/namespaces/{NS}/configmaps/{_sources_map()}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None, []
        raise
    rows = json.loads(cm.get("data", {}).get("sources.json", "[]"))
    if not isinstance(rows, list) or any(not isinstance(row, dict) or not SAFE.fullmatch(str(row.get("name", ""))) for row in rows):
        raise ValueError("Source inventory is invalid; refusing to replace it")
    if len({row["name"] for row in rows}) != len(rows):
        raise ValueError("Source inventory has duplicate names")
    return cm, rows


def list_sources():
    return _sources_state()[1]


def save_sources(srcs, current):
    name = _sources_map()
    body = {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": NS},
            "data": {"sources.json": json.dumps(srcs, indent=2)}}
    if current is not None:
        meta = current.get("metadata", {})
        if not meta.get("uid") or not meta.get("resourceVersion"):
            raise ValueError("Source inventory identity is unavailable")
        body["metadata"].update(uid=meta["uid"], resourceVersion=meta["resourceVersion"])
        return ksend("PUT", f"/api/v1/namespaces/{NS}/configmaps/{name}", body)
    return ksend("POST", f"/api/v1/namespaces/{NS}/configmaps", body)


def add_source(name, host, user, password, kind="unraid", base_path="/mnt/user/appdata", port=22):
    if not SAFE.fullmatch(name):
        raise ValueError("name must be lowercase letters, numbers and dashes")
    endpoint = SOURCE_SSH.connection({"host": host, "user": user, "port": port})
    if kind not in ("unraid", "proxmox", "ssh") or not str(base_path).startswith("/") or any(c in str(base_path) for c in "\r\n\0"):
        raise ValueError("Choose a source type and an absolute appdata path")
    with SOURCE_LOCK:
        cm, srcs = _sources_state()
        if any(row["name"] == name for row in srcs):
            raise ValueError("That source already exists; verify it, or use a new name for a different connection")
        # Immutable credentials keep a partial save or later reuse of a source
        # name from changing credentials underneath an existing copy Job.
        secret = f"homestead-src-{name}-{secrets.token_hex(4)}"
        sec = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque", "immutable": True,
               "metadata": {"name": secret, "namespace": NS, "labels": {"homestead.io/import-source": name}},
               "stringData": {"password": password or ""}}
        ksend("POST", f"/api/v1/namespaces/{NS}/secrets", sec)
        srcs.append({"name": name, **endpoint, "kind": kind, "base_path": base_path,
                     "credential_secret": secret, "added": time.strftime("%Y-%m-%d %H:%M")})
        save_sources(srcs, cm)  # failure retains the unused Secret, never adopts it
        return srcs


def del_source(name):
    with SOURCE_LOCK:
        cm, srcs = _sources_state()
        if not any(s["name"] == name for s in srcs):
            raise ValueError("Source no longer exists")
        srcs = [s for s in srcs if s["name"] != name]
        save_sources(srcs, cm)
        # Existing helpers retain their immutable credential reference. Removing
        # the directory entry is not credential revocation or Job cancellation.
        return srcs


def source_secret(name, src=None):
    """This source's password secret."""
    return (src or {}).get("credential_secret") or f"homestead-src-{name}"


def _source(name):
    s = next((x for x in list_sources() if x["name"] == name), None)
    if not s:
        raise ValueError(f"no import source named {name}")
    return s


def _trust_context(cm, src, actor):
    meta = (cm or {}).get("metadata", {})
    if not meta.get("uid") or not meta.get("resourceVersion"):
        raise ValueError("Source inventory identity is unavailable")
    return {"action": "source-trust", "actor": actor, "namespace": NS,
            "uid": meta["uid"], "version": meta["resourceVersion"], "source": src}


def scan_source(name, actor):
    cm, srcs = _sources_state()
    src = next((s for s in srcs if s["name"] == name), None)
    if src is None:
        raise ValueError("Source no longer exists")
    context = _trust_context(cm, src, actor)
    found = SOURCE_SSH.candidate(run_probe("key-" + name, SOURCE_SSH.scan_script(src), src, authenticated=False))
    config = {"name": name, "key": found["key"]}
    previous = (src.get("ssh_trust") or {}).get("fingerprint", "")
    return {**config, **found, "connection": SOURCE_SSH.connection(src), "previous": previous,
            "changed": bool(previous and previous != found["fingerprint"]),
            "capacity_token": SOURCE_REVIEW.issue(config, context)}


def trust_source(body, actor):
    with SOURCE_LOCK:
        cm, srcs = _sources_state()
        src = next((s for s in srcs if s["name"] == body.get("name")), None)
        if src is None:
            raise ValueError("Source no longer exists")
        context = _trust_context(cm, src, actor)
        found = SOURCE_SSH.key(body.get("key"))
        config = {"name": src["name"], "key": found["key"], "capacity_token": body.get("capacity_token")}
        if body.get("confirm_fingerprint") is not True or not SOURCE_REVIEW.valid(config, context):
            raise ValueError("Scan again and confirm the fingerprint from the source host's console")
        src["ssh_trust"] = {**found, "connection": SOURCE_SSH.connection(src), "by": actor,
                            "verified_at": time.strftime("%Y-%m-%d %H:%M")}
        save_sources(srcs, cm)
        return {"ok": True, "sources": srcs}


# --------------------------------------------------------------- discovery
def browse_source(name, path=None):
    """List candidate appdata directories on a source host, via a short-lived Job.

    We cannot SSH from the API server, so we run a pod that does it and read the
    result from its logs. Slower than a direct call but keeps credentials inside
    the cluster.
    """
    s = _source(name)
    p = path or s.get("base_path", "/mnt/user/appdata")
    remote_cmd = f"entries=$(ls -1 {shlex.quote(p)}) || exit $?; printf '%s\\n' \"$entries\" | head -200"
    script = _ssh_script(s, remote_cmd)
    return run_probe(f"browse-{name}", script, s)


def _ssh_script(src, remote_cmd):
    return SOURCE_SSH.command(src, remote_cmd)


def source_containers(name):
    """List Docker containers on an Unraid/generic Docker source.

    Docker's JSON line format gives us the real image reference instead of
    asking the user to remember or retype it during import.
    """
    src = _source(name)
    lines = run_probe(f"containers-{name}", _ssh_script(
        src, "entries=$(docker ps -a --format '{{json .}}') || exit $?; printf '%s\\n' \"$entries\" | head -200"), src)
    out = []
    for line in lines:
        try:
            row = json.loads(line)
        except Exception:
            continue
        nm = row.get("Names") or row.get("Name") or ""
        if nm:
            out.append({"name": nm, "image": row.get("Image", ""),
                        "state": row.get("State", ""), "status": row.get("Status", "")})
    return sorted(out, key=lambda x: x["name"].lower())


DEFAULT_SHM_MB = 64


def _shm_mb(size):
    """The /dev/shm a container was given, in MiB, if it was more than default."""
    try:
        megabytes = int(size or 0) // 1024 ** 2
    except (TypeError, ValueError):
        return 0
    return megabytes if megabytes > DEFAULT_SHM_MB else 0


def _tmpfs_mb(options):
    """The size a tmpfs mount was given, in MiB, or 0 when it was not."""
    match = re.search(r"size=(\d+)([kmg]?)", str(options or "").lower())
    if not match:
        return 0
    amount, unit = int(match.group(1)), match.group(2)
    scale = {"": 1 / 1024 ** 2, "k": 1 / 1024, "m": 1, "g": 1024}[unit]
    return max(1, int(amount * scale))


def inspect_source_container(name, container):
    """Translate Docker inspect fields into Homestead's deploy/import model."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", container or ""):
        raise ValueError("invalid container name")
    src = _source(name)
    lines = run_probe(f"inspect-{name}", _ssh_script(
        src, f"docker inspect {shlex.quote(container)} 2>/dev/null"), src)
    try:
        # Kubernetes combines a probe container's stdout and stderr.  SSH can
        # therefore put a host-key notice ahead of Docker's JSON on older
        # sources.  Decode the first JSON value instead of assuming byte zero.
        text = "\n".join(lines)
        starts = [i for i in (text.find("["), text.find("{")) if i >= 0]
        if not starts:
            raise ValueError("Docker returned no JSON")
        raw, _ = json.JSONDecoder().raw_decode(text[min(starts):])
        item = raw[0] if isinstance(raw, list) else raw
    except Exception as e:
        raise ValueError(f"could not inspect {container}: {e}")
    config, host = item.get("Config", {}) or {}, item.get("HostConfig", {}) or {}
    import homestead_privileges as PRIV
    privileges = PRIV.from_docker("", host.get("Privileged"), host.get("CapAdd") or (),
                                  [d.get("PathOnHost", "") for d in host.get("Devices") or []])
    env = {}
    for pair in config.get("Env", []) or []:
        key, sep, val = pair.partition("=")
        if sep and key not in ("PATH", "HOSTNAME", "HOME", "TERM"):
            env[key] = val
    ports = []
    bindings = host.get("PortBindings", {}) or {}
    exposed = config.get("ExposedPorts", {}) or {}
    for spec in sorted(set(bindings) | set(exposed)):
        num, _, proto = spec.partition("/")
        if not num.isdigit():
            continue
        vals = bindings.get(spec) or [{}]
        hp = next((x.get("HostPort") for x in vals if x.get("HostPort")), num)
        ports.append({"container": int(num), "host": int(hp),
                      "protocol": (proto or "tcp").upper(), "expose": True})
    mounts = [{"source": m.get("Source", ""), "path": m.get("Destination", ""),
               "type": m.get("Type", ""),
               "size_mb": _tmpfs_mb(m.get("Mode", "")) if m.get("Type") == "tmpfs" else 0}
              for m in item.get("Mounts", []) or [] if m.get("Destination")]
    # --tmpfs never appears in Mounts, only in HostConfig, and it is how
    # /tmp/cache is usually given to Frigate on Unraid.
    for destination, options in (host.get("Tmpfs", {}) or {}).items():
        if destination and not any(row["path"] == destination for row in mounts):
            mounts.append({"source": "", "path": destination, "type": "tmpfs",
                           "size_mb": _tmpfs_mb(options)})
    app_mount = _config_mount(mounts, src.get("base_path", "/mnt/user/appdata"))
    labels = config.get("Labels", {}) or {}
    devices = host.get("Devices", []) or []
    paths = " ".join([d.get("PathOnHost", "") for d in devices] + (host.get("Binds", []) or [])).lower()
    hardware = []
    for feature in hardware_features():
        host_path = feature.get("host_path", "").lower().rstrip("/")
        if host_path and (host_path in paths or any(
                str(d.get("PathOnHost", "")).lower().startswith(host_path + "/") for d in devices)):
            hardware.append(feature["id"])
    return {
        **privileges,
        "name": (item.get("Name") or container).lstrip("/"),
        "source_container_id": item.get("Id", ""),
        "source_running": bool((item.get("State") or {}).get("Running")),
        "image": config.get("Image", ""),
        "icon": (labels.get("net.unraid.docker.icon", "")
                 or labels.get("homestead.icon", "")),
        "webui": labels.get("net.unraid.docker.webui", ""),
        "env": env, "ports": ports, "mounts": mounts,
        "remote_path": app_mount["source"] if app_mount else "",
        "mount_path": app_mount["path"] if app_mount else "/config",
        # Nothing on the host said where this container keeps its config, so the
        # import must not pretend: a guessed path is one rsync cannot find.
        "guessed_path": not app_mount,
        "network_mode": host.get("NetworkMode", "bridge"), "hardware": hardware,
        "memory": str(host.get("MemoryReservation")) if int(host.get("MemoryReservation") or 0) > 0 else
                  str(min(256 * 1024 ** 2, int(host.get("Memory") or 256 * 1024 ** 2))),
        "memory_limit": str(host.get("Memory")) if int(host.get("Memory") or 0) > 0 else "",
        # Docker's --shm-size has no Kubernetes equivalent: a pod gets 64 MiB of
        # /dev/shm whatever it asks for. Frigate keeps every camera's frames
        # there, so a container given more on the source has to be given it
        # again here or ffmpeg hands over frames that never fit.
        "shm_mb": _shm_mb(host.get("ShmSize")),
    }


CONFIG_MOUNTS = ("/config", "/data", "/etc/config", "/var/lib")


def _config_mount(mounts, base_path):
    """The bind mount holding this container's configuration, or None.

    Usually it lives under the source's appdata directory. Plenty of Unraid
    templates keep it elsewhere - Frigate often sits beside the recordings on
    a camera share - so a container path that conventionally means config is
    the second choice. Guessing a path that is not there is not a choice at
    all: rsync only discovers that halfway through a copy.
    """
    binds = [m for m in mounts if m.get("source", "").startswith("/") and m.get("path")]
    under_base = [m for m in binds if m["source"].startswith(base_path.rstrip("/") + "/")
                  or m["source"] == base_path.rstrip("/")]
    if under_base:
        return under_base[0]
    return next((m for m in binds if m["path"].rstrip("/") in CONFIG_MOUNTS), None)


def run_probe(tag, script, src, timeout=70, *, authenticated=True):
    """Run a one-shot pod, wait for it, return its stdout."""
    setup = SOURCE_SSH.setup(src) if authenticated else ""
    pod = f"homestead-probe-{re.sub(r'[^a-z0-9-]', '-', tag)[:30]}-{secrets.token_hex(5)}"
    body = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": pod, "namespace": NS, "labels": NAMES.labels("probe")},
        "spec": {"restartPolicy": "Never", "terminationGracePeriodSeconds": 1,
                 "automountServiceAccountToken": False, "activeDeadlineSeconds": timeout,
                 "containers": [{
                     "name": "probe", "image": "alpine:3.24",
                     "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
                     "command": ["sh", "-c",
                                 "set -e\napk add --no-cache openssh-client sshpass >/dev/null 2>&1\n" + setup + script],
                     "env": [{"name": "SSHPASS", "valueFrom": {"secretKeyRef": {
                         "name": source_secret(src["name"], src), "key": "password"}}}] if authenticated else [],
                 }]},
    }
    receipt = ksend("POST", f"/api/v1/namespaces/{NS}/pods", body)
    uid = receipt.get("metadata", {}).get("uid")
    if not uid or receipt.get("metadata", {}).get("name") != pod:
        raise ValueError("Probe creation was not confirmed; no retry or name-based cleanup was attempted")
    out, deadline = "", time.time() + timeout
    try:
        while time.time() < deadline:
            time.sleep(2)
            current = kget(f"/api/v1/namespaces/{NS}/pods/{pod}")
            if current.get("metadata", {}).get("uid") != uid:
                raise ValueError("Probe identity changed; its result is not trusted")
            st = current.get("status", {})
            if st.get("phase") in ("Succeeded", "Failed"):
                if st["phase"] != "Succeeded":
                    raise ValueError("SSH probe failed. Check the source key, login and SSH service; changed keys require a new fingerprint review.")
                break
        else:
            raise ValueError("SSH probe timed out; no result was accepted")
        import urllib.request
        out = _pod_logs(pod)
        if kget(f"/api/v1/namespaces/{NS}/pods/{pod}").get("metadata", {}).get("uid") != uid:
            raise ValueError("Probe identity changed while reading logs; its result is not trusted")
    finally:
        try:
            ksend("DELETE", f"/api/v1/namespaces/{NS}/pods/{pod}",
                  {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": uid}})
        except Exception:
            pass
    return [l.strip() for l in out.splitlines() if l.strip()]


def _pod_logs(pod, ns=None):
    import urllib.request
    from homestead_shim import raw_get  # provided by server.py
    return raw_get(f"/api/v1/namespaces/{ns or NS}/pods/{pod}/log?tailLines=400")


# --------------------------------------------------------------- import job
MEASURE_LINE = re.compile(r"^(\d+)\s+(.*)$")


def measure_source_paths(name, paths, seconds=25):
    """Ask the source host how big each folder is, and stop asking if it drags.

    du walks every inode, which on a deep appdata tree can take longer than
    anyone wants to wait at a modal. Each path is measured under its own
    timeout so one slow folder costs that folder's answer rather than the
    whole measurement, and an unmeasured folder is reported as unknown instead
    of as zero.
    """
    src = _source(name)
    seconds = max(5, min(120, int(seconds or 25)))
    wanted = []
    for item in paths or []:
        candidate = str(item or "").strip().rstrip("/")
        if not candidate.startswith("/"):
            raise ValueError(f"path must be absolute, got {candidate or '(blank)'}")
        if candidate not in wanted:
            wanted.append(candidate)
    if not wanted:
        return {"paths": [], "total_bytes": 0, "complete": True, "suggested_gb": 1}

    # du -sk is the portable spelling: busybox and GNU both have it.
    commands = []
    for candidate in wanted:
        quoted = shlex.quote(candidate)
        commands.append(f"printf '%s\\n' {shlex.quote('### ' + candidate)}; [ -e {quoted} ] || echo MISSING; "
                        f"[ -e {quoted} ] && {{ timeout {seconds} du -sk {quoted} 2>/dev/null "
                        f"|| echo TIMEOUT; }}; true")
    lines = run_probe(f"measure-{name}", _ssh_script(src, "; ".join(commands)), src,
                      timeout=min(180, seconds * len(wanted) + 40))

    rows, current = [], None
    for line in lines:
        if line.startswith("### "):
            current = line[4:].strip()
            rows.append({"path": current, "bytes": None, "measured": False, "exists": True,
                         "timed_out": False})
            continue
        if not rows:
            continue
        if line.strip() == "MISSING":
            rows[-1]["exists"] = False
            continue
        if line.strip() in ("TIMEOUT", "UNKNOWN"):
            rows[-1]["timed_out"] = True
            continue
        match = MEASURE_LINE.match(line.strip())
        if match and rows[-1]["bytes"] is None:
            rows[-1].update(bytes=int(match.group(1)) * 1024, measured=True)
    _deduct_nested(rows)
    measured = [row for row in rows if row["measured"]]
    total = sum(row["bytes"] for row in measured)
    missing = [row["path"] for row in rows if not row["exists"]]
    complete = bool(rows) and len(measured) == len(rows)
    # Room for the copy plus what the app writes next; never below 1 GiB.
    suggested = max(1, int(total / (1024 ** 3) * 1.25) + 1)
    return {"paths": rows, "total_bytes": total, "complete": complete, "missing": missing,
            "suggested_gb": suggested, "timeout_seconds": seconds}


def _deduct_nested(rows):
    """du counts a subfolder inside its parent; the copy will not.

    Anything mapped separately is excluded from its parent's rsync, so the
    parent's measured size has to lose it too - otherwise a 4 TB recordings
    folder is counted once against the appdata volume it is not going to and
    once against the volume it is.
    """
    by_path = {row["path"]: row for row in rows if row["measured"]}
    for row in rows:
        if not row["measured"]:
            continue
        inside = sum(other["bytes"] for path, other in by_path.items()
                     if path.startswith(row["path"] + "/")
                     # Only the nearest parent deducts a folder, or a tree three
                     # deep would subtract the same bytes twice.
                     and not any(path.startswith(mid + "/") and mid.startswith(row["path"] + "/")
                                 for mid in by_path))
        if inside:
            row["gross_bytes"] = row["bytes"]
            row["bytes"] = max(0, row["bytes"] - inside)


def _ownership(cfg):
    """The uid:gid imported files should end up owned by, if any.

    rsync runs as root with --no-owner, so everything it writes lands owned by
    root. A container that runs as its own user - mosquitto as 1883, a
    linuxserver image as PUID - then cannot write to its own appdata.
    """
    def _id(value):
        text = str(value if value is not None else "").strip()
        if not text:
            return None
        if not text.isdigit() or not 0 <= int(text) <= 65535:
            raise ValueError("user and group ids must be whole numbers between 0 and 65535")
        return int(text)

    env = cfg.get("env") or {}
    uid = _id(cfg.get("uid") if cfg.get("uid") not in (None, "") else env.get("PUID"))
    gid = _id(cfg.get("gid") if cfg.get("gid") not in (None, "") else env.get("PGID"))
    if uid is not None and gid is None:
        gid = uid
    return uid, gid


def _numeric(value):
    text = str(value if value is not None else "").strip()
    return int(text) if text.isdigit() and 0 <= int(text) <= 65535 else None


def ownership_hint(namespace, pvc):
    """Who should own this claim's files, judged from whatever mounts it.

    The answer is never guessed: it comes from the workload itself, in the
    order of how explicit each source is - the PUID/PGID convention first,
    then a securityContext, then the pod's fsGroup. When nothing says, the
    mounting workload and its image are reported so the answer can be looked
    up rather than invented.
    """
    namespace = str(namespace or NS)
    try:
        deployments = kget(f"/apis/apps/v1/namespaces/{namespace}/deployments").get("items", [])
    except Exception:
        deployments = []
    for deployment in deployments:
        spec = ((deployment.get("spec", {}) or {}).get("template", {}) or {}).get("spec", {}) or {}
        mounted = {volume.get("name") for volume in spec.get("volumes", []) or []
                   if (volume.get("persistentVolumeClaim") or {}).get("claimName") == pvc}
        if not mounted:
            continue
        workload = deployment["metadata"]["name"]
        pod_security = spec.get("securityContext", {}) or {}
        for container in spec.get("containers", []) or []:
            if not any(mount.get("name") in mounted
                       for mount in container.get("volumeMounts", []) or []):
                continue
            env = {item.get("name"): item.get("value", "")
                   for item in container.get("env", []) or [] if item.get("name")}
            security = container.get("securityContext", {}) or {}
            image = container.get("image", "")
            candidates = (
                (_numeric(env.get("PUID")), _numeric(env.get("PGID")),
                 f"PUID/PGID on {container.get('name', workload)}"),
                (_numeric(security.get("runAsUser")), _numeric(security.get("runAsGroup")),
                 f"the container's security context in {workload}"),
                (_numeric(pod_security.get("runAsUser")), _numeric(pod_security.get("fsGroup")),
                 f"the pod security context in {workload}"),
                (None, _numeric(pod_security.get("fsGroup")), f"the fsGroup already set on {workload}"),
            )
            for uid, gid, source in candidates:
                if uid is None and gid is None:
                    continue
                return {"uid": uid, "gid": gid if gid is not None else uid,
                        "source": source, "workload": workload, "image": image,
                        "known": True}
            return {"uid": None, "gid": None, "workload": workload, "image": image,
                    "known": False,
                    "source": f"{workload} does not declare a user; check what {image or 'its image'} "
                              "runs as - mosquitto uses 1883, linuxserver images use PUID"}
    return {"uid": None, "gid": None, "known": False, "workload": "", "image": "",
            "source": "nothing mounts this volume, so its files can be owned by anyone you choose"}


def chown_claim(namespace, pvc, uid, gid):
    """Hand an existing claim to a user, for appdata already copied as root."""
    namespace = str(namespace or NS)
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", str(pvc or "")):
        raise ValueError("volume name must be lowercase letters, numbers and dashes")
    owner_uid, owner_gid = _ownership({"uid": uid, "gid": gid})
    if owner_uid is None:
        raise ValueError("a user id is required")
    job = NAMES.fitted(f"homestead-chown-{pvc}")
    try:
        existing = kget(f"/apis/batch/v1/namespaces/{namespace}/jobs/{job}")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        existing = None
    if existing:
        # Only this volume's last chown is replaced, never another volume's.
        if NAMES.label_of(existing.get("metadata") or {}, "app") != pvc:
            raise ValueError(f"the chown Job {job} belongs to another volume; try again once it is gone "
                             "(a finished one is removed within 10 minutes)")
        ksend("DELETE", f"/apis/batch/v1/namespaces/{namespace}/jobs/{job}"
                        "?propagationPolicy=Background")
        time.sleep(1)
    script = (f"echo '==> step 1/1 ownership :: chown -R {owner_uid}:{owner_gid} /data'\n"
              f"chown -R {owner_uid}:{owner_gid} /data\n"
              "echo '==> done'; ls -ld /data\n")
    body = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": job, "namespace": namespace,
                     "labels": NAMES.labels("chown", app=pvc)},
        "spec": {"backoffLimit": 1, "ttlSecondsAfterFinished": 600,
                 "template": {"metadata": {"labels": NAMES.labels("chown")},
                              "spec": {"restartPolicy": "Never",
                                       "containers": [{"name": "chown", "image": "alpine:3.24",
                                                       "command": ["sh", "-c", script],
                                                       "volumeMounts": [{"name": "data", "mountPath": "/data"}]}],
                                       "volumes": [{"name": "data",
                                                    "persistentVolumeClaim": {"claimName": pvc}}]}}},
    }
    ksend("POST", f"/apis/batch/v1/namespaces/{namespace}/jobs", body)
    _bust("vol", "stor")
    return {"ok": True, "job": job, "uid": owner_uid, "gid": owner_gid,
            "message": f"Setting ownership of {pvc} to {owner_uid}:{owner_gid}"}


def _folder_name(value, used):
    """A safe directory name inside the appdata volume."""
    base = re.sub(r"[^a-z0-9._-]+", "-", str(value or "").strip("/").split("/")[-1].lower())
    base = base.strip("-.")[:60] or "data"
    name, suffix = base, 2
    while name in used:
        name = f"{base}-{suffix}"
        suffix += 1
    used.add(name)
    return name


def import_volumes(cfg):
    """The claims this import writes into, in the order they were defined.

    Appdata and recordings do not belong on the same volume: one is small and
    wants replicas, the other is large and usually does not. An import may
    therefore land in several claims, and each folder says which one it goes to.
    """
    rows, seen = [], set()
    requested = cfg.get("volumes") or []
    copies = [row for row in (cfg.get("mappings") or [])
              if str(row.get("medium") or "").lower() != "memory"
              and (str(row.get("remote_path") or "").strip() or row.get("copy") is False)]
    if not requested and not copies and cfg.get("mappings") is not None:
        # Plenty of containers keep nothing: a webhook relay, a bridge, a
        # snapshot helper. Inventing an appdata claim for one creates storage
        # nobody asked for and a copy job with nothing to copy.
        return []
    if not requested:
        # The original shape: one claim for the whole import.
        requested = [{"name": cfg.get("pvc_name") or f"{cfg.get('name', 'app')}-appdata",
                      "create": not cfg.get("reuse_existing"),
                      "size_gb": cfg.get("size_gb", 10),
                      "storage_class": cfg.get("storage_class"),
                      "access_mode": cfg.get("access_mode")}]
    for item in requested:
        claim = str(item.get("name") or "").strip()
        if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", claim):
            raise ValueError(f"volume name {claim or '(blank)'} must be lowercase letters, "
                             "numbers and dashes")
        if claim in seen:
            raise ValueError(f"volume {claim} is listed twice")
        seen.add(claim)
        create = bool(item.get("create", True))
        size = int(item.get("size_gb") or 10)
        if create and not 1 <= size <= 16384:
            raise ValueError("volume size must be between 1 and 16384 GiB")
        access_mode = str(item.get("access_mode") or "ReadWriteOnce")
        if access_mode not in ("ReadWriteOnce", "ReadWriteMany"):
            raise ValueError("access mode must be ReadWriteOnce or ReadWriteMany")
        storage_class = str(item.get("storage_class") or "longhorn-r2").strip()
        if not re.fullmatch(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?", storage_class):
            raise ValueError("storage class must use lowercase letters, numbers, dots and dashes")
        rows.append({"name": claim, "create": create, "size_gb": size,
                     "storage_class": storage_class, "access_mode": access_mode})
    return rows


def import_mappings(cfg):
    """Every remote directory this import copies, and where it lands.

    An Unraid container usually maps several folders under appdata. They belong
    in one volume per container, each copied into its own subdirectory and
    mounted back at the path the container expects through subPath, rather than
    a volume per mapping.
    """
    rows, used, paths = [], set(), set()
    volumes = import_volumes(cfg)
    names = [volume["name"] for volume in volumes]
    requested = cfg.get("mappings")
    if requested is None:
        # The original single-folder shape, kept so older clients still work.
        requested = [{"remote_path": cfg.get("remote_path"),
                      "mount_path": cfg.get("mount_path", "/config"), "folder": ""}]
    if not requested:
        return []
    for item in requested:
        remote = str(item.get("remote_path") or "").strip().rstrip("/")
        mount = str(item.get("mount_path") or "").strip().rstrip("/") or "/config"
        if str(item.get("medium") or "").lower() == "memory":
            # A scratch mount has no source: the point of it is that it starts
            # empty and lives in RAM, exactly as tmpfs did on the source host.
            if mount in paths:
                raise ValueError(f"{mount} is mapped twice")
            paths.add(mount)
            size_mb = int(item.get("size_mb") or 1024)
            if not 1 <= size_mb <= 65536:
                raise ValueError("scratch size must be between 1 MiB and 64 GiB")
            rows.append({"remote_path": "", "mount_path": mount, "folder": "", "bytes": 0,
                         "pvc": "", "medium": "memory", "size_mb": size_mb})
            continue
        # Mounted empty: a volume the container gets without anything copied
        # into it - new recordings, say, where the old ones stay behind.
        copy = item.get("copy") is not False
        if copy and not remote.startswith("/"):
            raise ValueError(f"remote path must be absolute, got {remote or '(blank)'}")
        if copy and (any(part in (".", "..") for part in remote.split("/")) or
                     any(ord(char) < 32 for char in remote)):
            raise ValueError("remote path must not contain dot segments or control characters")
        if not mount.startswith("/"):
            raise ValueError(f"container path must be absolute, got {mount}")
        if mount in paths:
            raise ValueError(f"{mount} is mapped twice")
        paths.add(mount)
        target = str(item.get("pvc") or "").strip() or names[0]
        folder = str(item.get("folder") or "").strip("/")
        if folder:
            if not re.fullmatch(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*", folder):
                raise ValueError(f"folder {folder} must be a plain relative path")
            if any(part in (".", "..") for part in folder.split("/")):
                raise ValueError("folder cannot climb out of the volume")
            used.add((target, folder))
        elif sum(1 for row in requested
                 if str(row.get("medium") or "").lower() != "memory"
                 and (str(row.get("pvc") or "").strip() or names[0]) == target) > 1:
            # A volume receiving one folder takes it at its root; several
            # folders sharing a volume each get their own subdirectory.
            folder = _folder_name(remote or mount, {name for claim, name in used if claim == target})
            used.add((target, folder))
        try:
            size = max(0, int(item.get("bytes") or 0))
        except (TypeError, ValueError):
            size = 0
        claim = str(item.get("pvc") or "").strip() or names[0]
        if claim not in names:
            raise ValueError(f"{mount} points at volume {claim}, which this import does not create")
        rows.append({"remote_path": remote, "mount_path": mount, "folder": folder,
                     "bytes": size if copy else 0, "pvc": claim, "medium": "", "size_mb": 0, "copy": copy,
                     "exclude": _excludes(remote, requested, item.get("exclude")) if copy else []})
    destinations = [(row["pvc"], row["folder"]) for row in rows if not row.get("medium") and row.get("copy", True)]
    for index, (claim, folder) in enumerate(destinations):
        if any(claim == other and (not folder or not target or folder == target or
               folder.startswith(target + "/") or target.startswith(folder + "/"))
               for other, target in destinations[index + 1:]):
            raise ValueError("copy destinations overlap; choose separate folders")
    return rows


def _excludes(remote, requested, asked):
    """Subfolders of this folder that the copy must leave alone.

    Frigate on Unraid mounts /mnt/user/cctv/Frigate as its config and
    /mnt/user/cctv/Frigate/recordings as its recordings. Copying the first
    without saying otherwise drags the second along - into the wrong volume,
    twice over, terabytes of it. Anything mapped separately is excluded from
    its parent, whether or not that mapping is being copied: the client sends
    the folders it left unticked so those stay behind too.
    """
    nested = set()
    for other in requested:
        child = str(other.get("remote_path") or "").strip().rstrip("/")
        if child.startswith(remote + "/"):
            nested.add(child[len(remote):])
    for item in asked or []:
        relative = "/" + str(item or "").strip().strip("/")
        if relative == "/" or ".." in relative.split("/"):
            continue
        nested.add(relative)
    return sorted(nested)


def _claim_capacity_gb(pvc, ns=None):
    """What an existing claim actually offers, or 0 when it cannot be read."""
    try:
        claim = kget(f"/api/v1/namespaces/{ns or NS}/persistentvolumeclaims/{pvc}")
    except Exception:
        return 0.0
    quantity = ((claim.get("status", {}) or {}).get("capacity", {}) or {}).get("storage", "")
    try:
        return RESOURCES.quantity(quantity) / 1024 ** 3
    except ValueError:
        return 0.0


def prepare_import(cfg):
    """Build an import without creating claims, jobs, services or workloads.

    cfg: {source, name, image, ports, env, volumes[], mappings[], hardware,
          network_mode, start_after_copy, uid, gid}
    """
    name = cfg["name"]
    if not SAFE.match(name):
        raise ValueError("name must be lowercase letters, numbers and dashes")
    # Where the app, its volumes and its copy Job go. The source's password
    # lives beside the source, so a copy elsewhere borrows it (see dispatch).
    ns = cfg.get("namespace") or NS
    src = _source(cfg["source"])
    volumes = import_volumes(cfg)
    mappings = import_mappings(cfg)
    # What the job copies; a mapping mounted empty is only the workload's.
    copied = [row for row in mappings if not row.get("medium") and row.get("copy", True)]
    if copied:
        SOURCE_SSH.verified(src)
    pvc = volumes[0]["name"] if volumes else ""

    # Each volume is judged on what is going into it, not on the import total:
    # a 500 GiB recordings claim says nothing about whether appdata fits.
    if volumes:
        for volume in volumes:
            needed = sum(row.get("bytes") or 0 for row in mappings if row["pvc"] == volume["name"])
            if not needed:
                continue
            capacity_gb = float(volume["size_gb"])
            if not volume["create"]:
                capacity_gb = _claim_capacity_gb(volume["name"], ns)
                # Longhorn actualSize is physical snapshot allocation, not
                # guest filesystem usage. Free space is unknown here.
            needed_gb = needed / 1024 ** 3
            if capacity_gb and needed_gb > capacity_gb:
                raise ValueError(
                    f"the measured source needs {needed_gb:.1f} GiB but {volume['name']} has "
                    f"{capacity_gb:.1f} GiB total logical capacity"
                    + ". Grow the volume or choose a larger size, then import again.")

    for volume in volumes:
        if volume["create"]:
            continue
        try:
            existing = kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{volume['name']}")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise ValueError(f"existing PVC {volume['name']} was not found") from error
            raise
        modes = (existing.get("spec", {}) or {}).get("accessModes", []) or []
        volume["access_mode"] = modes[0] if modes else volume["access_mode"]
        volume["storage_class"] = (existing.get("spec", {}) or {}).get(
            "storageClassName", volume["storage_class"])
    storage_class = volumes[0]["storage_class"] if volumes else ""
    access_mode = volumes[0]["access_mode"] if volumes else ""

    job = f"homestead-import-{name}"
    password = source_secret(src["name"], src) if ns == NS else f"{job}-source"

    # Every interpolated value arrives from the UI, so all of it is quoted.
    # Each step announces itself on its own line before rsync's own progress,
    # which is what lets the UI say "2 of 5" instead of replaying 0-100% per
    # folder with nothing to say which folder it is on.
    steps = ["set -e",
             "apk add --no-cache rsync openssh-client sshpass >/dev/null 2>&1"]
    if copied:
        steps.append(SOURCE_SSH.setup(src))
    source_check = SOURCE_SSH.source_check(src, cfg) if copied else ""
    if source_check:
        steps.append(source_check)
    # rsync fails per folder, halfway through, after the volume exists. Asking
    # the source about all of them first turns that into one clear refusal.
    sources = [mapping["remote_path"] for mapping in copied]
    if sources:
        remote_check = "; ".join(f"[ -e {shlex.quote(source)} ] || echo {shlex.quote(source)}"
                                 for source in sources)
        steps.append("echo '==> checking the source folders exist'")
        steps.append("missing=$(" + _ssh_script(src, remote_check) + ")")
        steps.append('if [ -n "$missing" ]; then echo "==> missing $missing"; exit 4; fi')
    total = len(copied)
    # Measured up front, the copy can report bytes rather than folder counts.
    copied_rows = copied
    measured = sum(mapping.get("bytes") or 0 for mapping in copied_rows)
    if measured and copied_rows and all(mapping.get("bytes") for mapping in copied_rows):
        steps.append("echo " + shlex.quote(f"==> total {total} folders {measured}B"))
    mount_of = {volume["name"]: (f"/mnt/{volume['name']}" if len(volumes) > 1 else "/appdata")
                for volume in volumes}
    if copied:
        steps.append(COPY_CHECKS.SHELL)
        # Check every destination before the first write. Client measurements
        # are estimates only; existing files/snapshots are not free capacity.
        for mapping in copied:
            base = mount_of[mapping["pvc"]]
            target = base + ("/" + mapping["folder"] if mapping["folder"] else "")
            steps.append(f"copy_destination {shlex.quote(target)} {shlex.quote(base)}")
        for claim in sorted({mapping["pvc"] for mapping in copied}):
            rows = [mapping for mapping in copied if mapping["pvc"] == claim]
            needed = str((sum(row["bytes"] for row in rows) + 1023) // 1024) if all(row["bytes"] for row in rows) else ""
            steps.append(f"copy_space {shlex.quote(mount_of[claim])} {shlex.quote(needed)}")
    for index, mapping in enumerate(copied, start=1):
        base = mount_of[mapping["pvc"]]
        target = (base + "/" + mapping["folder"]) if mapping["folder"] else base
        host = f"[{src['host']}]" if ":" in src["host"] else src["host"]
        spec = shlex.quote(f"{src['user']}@{host}:{mapping['remote_path']}/")
        label = mapping["folder"] or (mapping["pvc"] if len(volumes) > 1 else "appdata")
        steps.append(f"mkdir -p {shlex.quote(target)}")
        weight = f" ({mapping['bytes']}B)" if mapping.get("bytes") else ""
        steps.append("echo " + shlex.quote(
            f"==> step {index}/{total} {label}{weight} :: {src['host']}:{mapping['remote_path']}"
            f" -> {mapping['mount_path']}"))
        # Said out loud, so a copy that comes back smaller than the folder looks
        # is explained by the log rather than by a support question.
        if mapping.get("exclude"):
            steps.append("echo " + shlex.quote(
                "    leaving out " + ", ".join(mapping["exclude"]) + " (mapped separately)"))
        steps.append(
            # Ownership and permissions are preserved by number, so the app
            # finds its appdata exactly as it was on the source. Dropping them
            # made every file root-owned, which is why an imported container
            # could not write to data a fresh deployment would have created
            # itself.
            "sshpass -e rsync -aH --protect-args --numeric-ids --info=progress2 "
            + "-e " + shlex.quote(SOURCE_SSH.transport(src)) + " "
            # A leading slash anchors the pattern to the folder being copied,
            # so /recordings means that one and not every directory so named.
            + "".join(f"--exclude={shlex.quote(pattern + '/')} "
                      for pattern in mapping.get("exclude") or [])
            + f"{spec} {shlex.quote(target + '/')}")
        steps.append("echo " + shlex.quote(f"==> step {index}/{total} {label} complete"))
        if source_check:
            steps.append(source_check)
    # Only when asked: the copy already keeps whatever the source had.
    owner_uid, owner_gid = _ownership(cfg)
    if owner_uid is not None:
        steps.append("echo " + shlex.quote(f"==> owner {owner_uid}:{owner_gid}"))
        for mount in sorted(set(mount_of.values())):
            steps.append(f"chown -R {owner_uid}:{owner_gid} {shlex.quote(mount)}")
    steps.append("sync; du -sh " + " ".join(
        shlex.quote(mount) for mount in sorted(set(mount_of.values()))))
    steps.append("echo '==> done'")
    script = "\n".join(steps) + "\n"

    body = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": job, "namespace": ns,
                     "labels": NAMES.labels("import", app=name),
                     # What this import made, so cleaning it up later does not
                     # have to guess - and cannot offer to delete a volume it
                     # merely borrowed.
                     "annotations": {"homestead.io/import-workload": name if cfg.get("create_workload", True) else "",
                                     "homestead.io/import-volume": pvc,
                                     "homestead.io/import-volume-created":
                                         "true" if volumes and volumes[0]["create"] else "false",
                                     "homestead.io/import-volumes-created":
                                         ",".join(v["name"] for v in volumes if v["create"]),
                                     "homestead.io/import-volumes":
                                         ",".join(v["name"] for v in volumes)}},
        # Retain the result until deliberate cleanup: a later start review
        # must distinguish a completed copy from a failed or missing Job.
        "spec": {"backoffLimit": 0,
                 "template": {"metadata": {"labels": NAMES.labels("import")},
                              "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                                       "containers": [{
                                           "name": "copy", "image": "alpine:3.24",
                                           "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                                                         "limits": {"memory": "512Mi"}},
                                           "command": ["sh", "-c", script],
                                           "env": [{"name": "SSHPASS", "valueFrom": {"secretKeyRef": {
                                               "name": password, "key": "password"}}}],
                                           "volumeMounts": [
                                               {"name": f"vol{index}", "mountPath": mount_of[volume["name"]]}
                                               for index, volume in enumerate(volumes)],
                                       }],
                                       "volumes": [
                                           {"name": f"vol{index}",
                                            "persistentVolumeClaim": {"claimName": volume["name"]}}
                                           for index, volume in enumerate(volumes)]}}},
    }
    # A container with nothing to copy needs no copy job; the workload below
    # is the whole import.
    dep = svc = None
    created = None
    if cfg.get("create_workload", True):
        dcfg = {
            "name": name, "namespace": ns, "image": cfg["image"],
            # An application must never write into a claim while rsync is
            # importing it. Starting later goes through a fresh start review.
            "replicas": 0 if copied or cfg.get("start_after_copy", True) else 1,
            "cpu": cfg.get("cpu", "50m"), "memory": cfg.get("memory", "256Mi"),
            "memory_limit": cfg.get("memory_limit", ""),
            "ports": cfg.get("ports") or [], "env": cfg.get("env") or {},
            "volumes": [
                {"path": mapping["mount_path"], "type": "emptyDir", "medium": "memory",
                 "size_limit": f"{mapping['size_mb']}Mi"}
                if mapping.get("medium") else
                {"path": mapping["mount_path"], "source": mapping["pvc"], "type": "pvc",
                 "sub_path": mapping["folder"]}
                for mapping in mappings],
            "fs_group": owner_gid,
            "gpu": bool(cfg.get("gpu")),
            "hardware": cfg.get("hardware") or [], "icon": cfg.get("icon", ""),
            "icon_source": cfg.get("icon_source", cfg.get("icon", "")),
            "network_mode": cfg.get("network_mode", "loadbalancer"),
            "vip_mode": cfg.get("vip_mode", "shared"), "lb_ip": cfg.get("lb_ip", ""),
            "privileged": bool(cfg.get("privileged")), "cap_add": cfg.get("cap_add") or [],
            "tun": bool(cfg.get("tun")),
        }
        dep, svc = build_deployment(dcfg)
        if copied:
            dep.setdefault("metadata", {}).setdefault("annotations", {})["homestead.io/import-job"] = job
        created = name
    result = {"ok": True, "job": job if copied else "", "pvc": pvc, "deployment": created,
            "mappings": mappings, "volumes": volumes,
            "storage_class": storage_class, "access_mode": access_mode,
            "note": "Workload created stopped. After copying, review capacity again before starting."
                    if copied or cfg.get("start_after_copy", True) else ""}
    return {"job": body if copied else None, "deployment": dep, "service": svc,
            "volumes": volumes, "result": result, "namespace": ns,
            # Copied beside the Job for as long as the copy runs: it never
            # outlives the Job, and is removed once the copy has finished.
            "source_secret": {"namespace": NS, "name": source_secret(src["name"], src), "copy": password}
                             if copied and ns != NS else None}


def commit_import(prepared):
    """Commit a reviewed import; never replace an existing Job or Deployment.

    Partial resources are deliberately retained on failure, especially PVCs.
    The stopped workload is created before a copy can start writing data.
    """
    prepared = copy.deepcopy(prepared)
    ns = prepared.get("namespace") or NS
    for volume in prepared["volumes"]:
        if volume["create"]:
            create_pvc(ns, volume["name"], volume["size_gb"], volume["storage_class"],
                       volume["access_mode"])
    if prepared["deployment"]:
        created = ksend("POST", f"/apis/apps/v1/namespaces/{ns}/deployments", prepared["deployment"])
        uid = (created.get("metadata") or {}).get("uid")
        if prepared["job"] and not uid:
            raise ValueError("Created workload identity is unavailable; copy was not started. Keep its volumes and inspect the workload before retrying.")
        if prepared["job"] and uid:
            prepared["job"]["metadata"]["ownerReferences"] = [{"apiVersion": "apps/v1", "kind": "Deployment",
                "name": created["metadata"]["name"], "uid": uid}]
    if prepared["service"]:
        ksend("POST", f"/api/v1/namespaces/{ns}/services", prepared["service"])
    if prepared["job"]:
        ksend("POST", f"/apis/batch/v1/namespaces/{ns}/jobs", prepared["job"])
    _bust("wl", "ov", "flow")
    return prepared["result"]


def import_container(cfg):
    """Internal compatibility wrapper; HTTP imports must use signed admission."""
    return commit_import(prepare_import(cfg))


def import_inventory(prepared, read):
    """Fail closed on collisions and active writers; return identities to sign."""
    ns = prepared.get("namespace") or NS
    def optional(path):
        try:
            obj = read(path)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise
        if not isinstance(obj, dict) or not (obj.get("metadata") or {}).get("uid"):
            raise ValueError("Import inventory is incomplete; refresh before importing")
        return obj

    for kind, resource in (("deployment", "deployments"), ("service", "services"), ("job", "jobs")):
        obj = prepared[kind]
        if obj:
            prefix = "/apis/apps/v1" if kind == "deployment" else "/apis/batch/v1" if kind == "job" else "/api/v1"
            name = obj["metadata"]["name"]
            if optional(f"{prefix}/namespaces/{ns}/{resource}/{name}") is not None:
                raise ValueError(f"{kind} {name} already exists. Review or clean up that import first; it will not be replaced.")
    claims = {}
    for volume in prepared["volumes"]:
        name = volume["name"]
        obj = optional(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}")
        if volume["create"]:
            if obj is not None:
                raise ValueError(f"PVC {name} already exists. Explicitly select an existing volume or choose a new name.")
            claims[name] = None
        else:
            if obj is None:
                raise ValueError(f"existing PVC {name} was not found")
            if obj["metadata"].get("deletionTimestamp") or (obj.get("status") or {}).get("phase") != "Bound":
                raise ValueError(f"existing PVC {name} must be Bound and not deleting before import")
            if (obj.get("spec") or {}).get("volumeMode", "Filesystem") != "Filesystem":
                raise ValueError(f"PVC {name} is not a filesystem volume")
            claims[name] = {"uid": obj["metadata"]["uid"], "spec": obj.get("spec")}
    pods = read("/api/v1/pods")
    if not isinstance(pods.get("items"), list) or (pods.get("metadata") or {}).get("continue"):
        raise ValueError("Pod inventory is incomplete; volume consumers cannot be verified")
    if prepared["job"]:
        for pod in pods["items"]:
            if (pod.get("metadata") or {}).get("namespace") != ns or (pod.get("status") or {}).get("phase") in ("Succeeded", "Failed"):
                continue
            mounted = {(row.get("persistentVolumeClaim") or {}).get("claimName") for row in (pod.get("spec") or {}).get("volumes") or []}
            conflicts = mounted & claims.keys()
            if conflicts:
                raise ValueError("Stop volume consumers before importing: " + ", ".join(sorted(conflicts)) +
                                 f" is mounted by {pod['metadata'].get('name', '?')}. RWX does not make concurrent data replacement safe.")
    return {"claims": claims}, pods["items"]


STEP = re.compile(r"==> step (\d+)/(\d+) (\S+)(?: \((\d+)B\))?\s*(.*)$")
TOTAL = re.compile(r"==> total (\d+) folders (\d+)B")
# What went wrong, in the words the log used, so the UI need not say "failed".
TROUBLE = (
    ("No such file or directory", "a source folder does not exist on the host"),
    ("No space left on device", "ran out of space on the volume"),
    ("Permission denied", "the source refused the credentials"),
    ("Host key verification failed", "the source host key was rejected"),
    ("Connection refused", "the source host refused the connection"),
    ("Connection timed out", "the source host did not answer"),
    ("No route to host", "the source host was unreachable"),
    ("failed to resolve", "the source host name could not be resolved"),
)
# rsync --info=progress2 redraws one line with \r: "  1,234,567  57%  11.83MB/s  0:00:04"
RSYNC = re.compile(r"([\d,]+)\s+(\d+)%\s+(\S+)\s+(\d+:\d\d:\d\d)")


def import_progress(log):
    """Read the copy job's output as one overall position, not a replayed bar.

    rsync restarts its percentage for every folder it copies, which is why the
    log looks like 0-100% several times over. Each folder is announced first,
    so the step counter carries the real progress and rsync's percentage only
    fills in the step that is running.
    """
    step, total, label, detail, percent, rate = 0, 0, "", "", 0, ""
    done_steps = 0
    sizes, done_bytes, total_bytes = {}, 0, 0
    error, error_detail = "", ""
    for line in str(log or "").replace("\r", "\n").splitlines():
        line = line.strip()
        header = TOTAL.match(line)
        if header:
            total, total_bytes = int(header.group(1)), int(header.group(2))
            continue
        match = STEP.match(line)
        if match:
            step, total = int(match.group(1)), int(match.group(2))
            label, rest = match.group(3), (match.group(5) or "").strip()
            if match.group(4):
                sizes[step] = int(match.group(4))
            if rest == "complete":
                done_steps, percent, rate = step, 100, ""
                done_bytes = sum(sizes.get(index, 0) for index in range(1, step + 1))
            else:
                detail = rest[2:].strip() if rest.startswith("::") else rest
                percent, rate = 0, ""
            continue
        if line.startswith("==> missing "):
            error = "a source folder does not exist on the host"
            error_detail = line[len("==> missing "):][:220]
            continue
        if line.startswith("==> error: "):
            error = "copy preflight failed"
            error_detail = line[len("==> error: "):][:220]
            continue
        for needle, explanation in TROUBLE:
            if needle.lower() in line.lower():
                error, error_detail = explanation, line[:220]
                break
        if not error and line.startswith("rsync error:"):
            error, error_detail = "the copy failed", line[:220]
        moved = RSYNC.search(line)
        if moved:
            percent, rate = int(moved.group(2)), moved.group(3)
        elif line.startswith("==> done"):
            step = total = max(total, step)
            done_steps, percent = total, 100
    if not total:
        # A copy can fail before it announces a step - a source that is not
        # there, a host that will not answer - and that reason still matters.
        return {"error": error, "error_detail": error_detail, "percent": 0} if error else {}
    running = percent / 100 if done_steps < step else 0
    measured = total_bytes > 0
    if measured:
        # Folders differ wildly in size, so count bytes when the import knows
        # them: four folders are not four equal quarters of the copy.
        overall = (done_bytes + sizes.get(step, 0) * running) / total_bytes * 100
    else:
        overall = (done_steps + running) / total * 100
    return {"step": step, "steps": total, "folder": label, "detail": detail,
            "step_percent": percent, "percent": round(min(100.0, max(0.0, overall)), 1),
            "rate": rate, "weighted": measured, "total_bytes": total_bytes,
            "error": error, "error_detail": error_detail}


def _job_ns(ns):
    """The namespace an import job was named in: its own, or the default."""
    ns = str(ns or NS)
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", ns):
        raise ValueError("unknown namespace")
    return ns


def import_cleanup_plan(name, ns=None):
    """What an import job left behind, read from the job itself."""
    if not re.fullmatch(r"homestead-import-[a-z0-9][a-z0-9-]{0,60}", str(name or "")):
        raise ValueError("unknown import job")
    ns = _job_ns(ns)
    try:
        job = kget(f"/apis/batch/v1/namespaces/{ns}/jobs/{name}")
    except Exception:
        if IMPORT_JOB.journalled(ns, name, {}, OPS):
            return {"job": name, "namespace": ns, "workload": "", "volume": "", "volumes": [], "known": True, "journalled": True}
        return {"job": name, "workload": "", "volume": "", "volume_created": False,
                "volumes": [], "namespace": ns, "known": False}
    if IMPORT_JOB.journalled(ns, name, job, OPS):
        return {"job": name, "namespace": ns, "workload": "", "volume": "", "volumes": [], "known": True, "journalled": True}
    meta = job.get("metadata", {}) or {}
    annotations = meta.get("annotations", {}) or {}
    app = NAMES.label_of(meta, "app")
    workload = annotations.get("homestead.io/import-workload", app)
    volume = annotations.get("homestead.io/import-volume", "")
    created = str(annotations.get("homestead.io/import-volume-created", "")).lower() == "true"
    exists = False
    if workload:
        try:
            kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{workload}")
            exists = True
        except Exception:
            exists = False
    return {"job": name, "namespace": ns, "workload": workload if exists else "",
            "volume": volume, "volume_created": created,
            # An import can fill several volumes - appdata on one, recordings on
            # another - and cleaning it up has to offer all of them, not just the
            # first one it happened to record.
            "volumes": _plan_volumes(annotations, volume, created),
            # An import that predates this annotation says so rather than
            # implying the volume is safe to delete.
            "known": bool(annotations)}


def _names(annotations, key):
    return [part.strip() for part in str(annotations.get(key, "") or "").split(",") if part.strip()]


def _plan_volumes(annotations, volume, created):
    """Every claim this import used, and whether it is this import's to delete."""
    made = set(_names(annotations, "homestead.io/import-volumes-created"))
    used = _names(annotations, "homestead.io/import-volumes") or sorted(made)
    if not used and volume:
        # An import from before either list, which recorded one claim only.
        return [{"name": volume, "created": created}]
    return [{"name": claim, "created": claim in made} for claim in used]


def delete_import(name, ns=None):
    """Remove an import Job, cancelling the copy if it is still running.

    A failed import leaves its Job behind, and while that Job exists it still
    references the appdata claim - which used to leave both the import and the
    volume it was filling unremovable from the UI.
    """
    if not re.fullmatch(r"homestead-import-[a-z0-9][a-z0-9-]{0,60}", str(name or "")):
        raise ValueError("unknown import job")
    ns = _job_ns(ns)
    # Preserve successful completion before removing its proof. Failed copies
    # deliberately leave the workload interlocked, never silently startable.
    try:
        job = kget(f"/apis/batch/v1/namespaces/{ns}/jobs/{name}")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        job = {}
    except ValueError:
        job = {}  # legacy client reports a missing Job this way
    if IMPORT_JOB.journalled(ns, name, job, OPS):
        return IMPORT_JOB.remove_completed_job(ns, name, job, kget, ksend, OPS)
    complete = any(c.get("type") == "Complete" and c.get("status") == "True"
                   for c in (job.get("status") or {}).get("conditions") or [])
    for owner in (job.get("metadata") or {}).get("ownerReferences") or []:
        if not complete or owner.get("kind") != "Deployment":
            continue
        path = f"/apis/apps/v1/namespaces/{ns}/deployments/{owner['name']}"
        try:
            dep = kget(path)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                continue
            raise
        if dep["metadata"].get("uid") != owner.get("uid"):
            raise ValueError("Import workload identity changed; review cleanup again")
        annotations = dep["metadata"].get("annotations") or {}
        if annotations.get("homestead.io/import-job") == name:
            annotations.pop("homestead.io/import-job")
            annotations["homestead.io/import-completed-job"] = job["metadata"]["uid"]
            dep["metadata"]["annotations"] = annotations
            ksend("PUT", path, dep)  # resourceVersion protects concurrent edits
    try:
        ksend("DELETE", f"/apis/batch/v1/namespaces/{ns}/jobs/{name}"
                        "?propagationPolicy=Background")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    stopped = _stop_job_pods(name, ns=ns)
    _drop_source_copy(name, ns)
    # A pre-pull started for this app outlives the import that wanted it: it is
    # a DaemonSet, so deleting its pods only makes it build new ones.
    app = re.sub(r"^homestead-import-", "", name)
    pulls = [f"{prefix}{app}" for prefix in PREPULL_NAMES]
    stopped_pulls = [pull for pull in pulls if _daemonset_exists(pull)]
    for pull in stopped_pulls:
        stop_prepull(pull)
    _bust("wl", "ov")
    return {"ok": True, "name": name, "pods_removed": stopped,
            "prepulls_removed": stopped_pulls,
            "message": f"Import {name} removed"}


def _daemonset_exists(name):
    try:
        found = kget(f"/apis/apps/v1/namespaces/{NS}/daemonsets/{name}")
    except Exception:
        return False
    return bool((found.get("metadata", {}) or {}).get("name"))


def wait_for_pods_gone(namespace, selector, seconds=20):
    """Wait for pods matching a selector to disappear, and say whether they did."""
    deadline = time.time() + max(0, seconds)
    while True:
        try:
            pods = kget(f"/api/v1/namespaces/{namespace}/pods"
                        f"?labelSelector={urllib.parse.quote(selector)}").get("items", [])
        except Exception:
            return True
        if not pods or time.time() >= deadline:
            return not pods
        time.sleep(1)


def _stop_job_pods(job, seconds=20, ns=None):
    """Kill the copy pod now, and wait for it to actually be gone.

    Deleting the Job in the background hands the pod to the garbage collector
    and returns at once, so an rsync that was running kept running - holding
    the claims open, which left any volume deleted alongside it stuck
    Terminating behind the pvc-protection finaliser. Cancelling a copy means
    cancelling it, so the pod goes first and the claims are free afterwards.
    """
    removed, ns = [], ns or NS
    try:
        pods = kget(f"/api/v1/namespaces/{ns}/pods?labelSelector=job-name%3D{job}").get("items", [])
    except Exception:
        return removed
    for pod in pods:
        pod_name = pod["metadata"]["name"]
        try:
            ksend("DELETE", f"/api/v1/namespaces/{ns}/pods/{pod_name}?gracePeriodSeconds=0")
            removed.append(pod_name)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
    deadline = time.time() + max(0, seconds)
    while removed and time.time() < deadline:
        try:
            left = kget(f"/api/v1/namespaces/{ns}/pods?labelSelector=job-name%3D{job}").get("items", [])
        except Exception:
            break
        if not left:
            break
        time.sleep(1)
    return removed


def _drop_source_copy(job, ns):
    """The source password borrowed for a copy in another namespace, once the
    copy no longer needs it. Only ever the copy: the original stays."""
    if ns == NS:
        return
    try:
        ksend("DELETE", f"/api/v1/namespaces/{ns}/secrets/{job}-source")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise


def _drop_finished_copies():
    """Each finished copy in another namespace gives back the source password
    it borrowed. The leader's, once a minute (tick): reading the imports does
    not (#622)."""
    for job in NAMES.find("/apis/batch/v1/jobs", "task"):
        meta, status = job["metadata"], job.get("status", {})
        ns = meta.get("namespace") or NS
        if NAMES.label_of(meta, "task") not in ("import", "chown") or ns == NS or status.get("active") \
                or not (status.get("succeeded") or status.get("failed")):
            continue
        try:
            _drop_source_copy(meta["name"], ns)
        except Exception as error:
            print(f"import {ns}/{meta['name']}: its source password was not removed: {str(error)[:160]}", flush=True)


def _job_pod(job, ns=None):
    try:
        pods = kget(f"/api/v1/namespaces/{ns or NS}/pods?labelSelector=job-name%3D{job}").get("items", [])
    except Exception:
        return ""
    pods.sort(key=lambda pod: pod["metadata"].get("creationTimestamp", ""), reverse=True)
    return pods[0]["metadata"]["name"] if pods else ""


def import_status():
    try:
        # Every namespace: an import can be put in any of them.
        jobs = [job for job in
                NAMES.find("/apis/batch/v1/jobs", "task")
                if NAMES.label_of(job["metadata"], "task") in ("import", "chown")]
    except Exception:
        return []
    out = []
    for j in jobs:
        st = j.get("status", {})
        ns = j["metadata"].get("namespace") or NS
        state = ("running" if st.get("active") else "done" if st.get("succeeded")
                 else "failed" if st.get("failed") else "pending")
        row = {
            "name": j["metadata"]["name"], "namespace": ns,
            "kind": NAMES.label_of(j["metadata"], "task", "import"),
            "app": NAMES.label_of(j["metadata"], "app"),
            "active": st.get("active", 0), "succeeded": st.get("succeeded", 0),
            "failed": st.get("failed", 0),
            "start": st.get("startTime", ""), "end": st.get("completionTime", ""),
            "state": state,
        }
        if state in ("running", "failed"):
            pod = _job_pod(j["metadata"]["name"], ns)
            if pod:
                try:
                    log = _pod_logs(pod, ns)
                    row.update(import_progress(log) or {})
                    if state == "failed" and not row.get("error"):
                        tail = [line for line in str(log or "").splitlines() if line.strip()]
                        row["error_detail"] = tail[-1][:220] if tail else ""
                        row["error"] = "the copy failed"
                except Exception:
                    pass
        elif state == "done":
            row["percent"] = 100
        out.append(row)
    return sorted(out, key=lambda x: x["start"], reverse=True)


# Moved by feature (#623). Re-exported, so other modules, server.py and tests
# that use or replace these names here keep working.
from homestead_vm_create import (  # noqa: E402,F401
    VM_DISK_LABEL, _required_name, _get_or_none, vm_disk_import_plan, _http_disk_source,
    import_vm_disk, _disk_consumers, list_vm_disks, _epoch, list_vm_images, LH_API, _items_or_empty,
    vm_image_cache, delete_vm_image, _harvester_image, _modes_for_cdi, _vm_mac, static_network,
    prepare_vm, _recheck_vm_creation, commit_vm, create_vm,
)
from homestead_scheduled_jobs import (  # noqa: E402,F401
    CRONJOB_NAME, _job_managed, _job_managed_by, list_jobs, _managed_job, save_job, del_job,
    run_job_now,
)
from homestead_image_cache import (  # noqa: E402,F401
    _canonical_image, _image_digest, _system_image, _needed_list, _group_absent,
    image_retention_inventory, _stopped_references, _holds, image_cache, SCAN_TASK, SCAN_FRESH,
    SCAN_RETRY, SCAN_STORE, _SCANS, _SCAN_FAILED, _SCAN_STARTED, _scan_lock, _scan_path,
    _load_scans, _save_scans, _merged, tick, _scannable, _rescan_if_stale, start_image_scan,
    _collect_scans, _scanned_images, PREPULL_NAMES, _pullable_nodes, prepull, prepull_status,
    stop_prepull, forget_rollback, cleanup_image,
)


# server.py's helpers the routes below call, bound by name (bind_routes):
# an import's capacity review lives there still.
ROUTE_NEEDS = ("guard_managed_smb", "preview_import", "reviewed_import", "workload_service_names")


def bind_routes(helpers):
    missing = set(ROUTE_NEEDS) - set(helpers)
    if missing:
        raise TypeError("homestead_imports needs " + ", ".join(sorted(missing)))
    globals().update({name: helpers[name] for name in ROUTE_NEEDS})


def _prepull(request):
    b = request.body
    result = prepull(b["image"], b.get("nodes"))
    result["operation"] = OPS.start(
        "image-pull", f"Pull {b['image']}",
        {"kind": "Image", "name": b["image"], "namespace": NS},
        "/image-cache", {"namespace": NS, "name": result["daemonset"]})
    return result


def _cleanup_image(request):
    b = request.body
    result = cleanup_image(b.get("digest"), b.get("nodes"))
    result["operation"] = OPS.start(
        "image-cleanup", f"Clean cached image {result['digest'][:19]}…",
        {"kind": "Image", "name": result["image"], "namespace": NS},
        "/image-cache", {"namespace": NS, "pods": result["pods"]})
    return result


def _delete_vm_image(request):
    homestead_routes.forget("vmimages")
    b = request.body
    return delete_vm_image(b.get("namespace", ""), b.get("name", ""), b.get("confirm", ""))


def _delete_import(request):
    """An import's record, and what it made that the request also names."""
    b = request.body
    plan = import_cleanup_plan(b.get("name"), b.get("namespace"))
    if plan.get("journalled") and any(b.get(key) for key in ("remove_workload", "remove_volume", "remove_volumes")):
        raise ValueError("This import retains workloads and volumes. Manage them separately after inspecting the copy.")
    made = {row["name"] for row in plan["volumes"] if row["created"]}
    # The request names claims; asking for all of them is the old
    # boolean, which an import with one volume still sends.
    wanted = [str(claim) for claim in (b.get("remove_volumes") or [])]
    if b.get("remove_volume") and not wanted:
        wanted = [row["name"] for row in plan["volumes"] if row["created"]]
    borrowed = [claim for claim in wanted if claim not in made]
    if borrowed:
        raise ValueError(f"this import copied into {', '.join(sorted(borrowed))} "
                         "without creating it, so it will not delete it")
    if b.get("remove_workload") and plan["workload"]:
        guard_managed_smb(plan["namespace"], plan["workload"])
    result = delete_import(b.get("name"), b.get("namespace"))
    if result.get("journalled"):
        return {**result, "removed": [], "releasing": []}
    removed = []
    if b.get("remove_workload") and plan["workload"]:
        ns, name = plan["namespace"], plan["workload"]
        services = set(workload_service_names(ns, name)) | {name}
        ksend("DELETE", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
        for service in sorted(services):
            try:
                ksend("DELETE", f"/api/v1/namespaces/{ns}/services/{service}")
            except urllib.error.HTTPError:
                pass
        removed.append(f"workload {name}")
        # Its pod holds the same claims the caller may be deleting
        # next, so wait for it to go rather than leaving them
        # Terminating behind the pvc-protection finaliser.
        wait_for_pods_gone(ns, f"app={name}")
        homestead_routes.forget("wl", "ov", "network")
    for claim in wanted:
        ksend("DELETE", f"/api/v1/namespaces/{plan['namespace']}/"
                        f"persistentvolumeclaims/{claim}")
        removed.append(f"volume {claim}")
        homestead_routes.forget("vol", "stor")
    # A claim something still mounts only gets a deletion stamp, so
    # say it is releasing rather than reporting it gone.
    releasing = []
    for claim in wanted:
        try:
            live = kget(f"/api/v1/namespaces/{plan['namespace']}/"
                        f"persistentvolumeclaims/{claim}")
        except Exception:
            continue
        if (live.get("metadata", {}) or {}).get("deletionTimestamp"):
            releasing.append(claim)
    result["releasing"] = releasing
    if removed:
        result["message"] = result["message"] + " with " + " and ".join(removed)
    if result.get("releasing"):
        result["message"] += (" — " + ", ".join(result["releasing"])
                              + " will finish deleting once released")
    result["removed"] = removed
    return result


# Its routes and who may use them (homestead_routes.py).
def _schedule_saved(request):
    save_job(request.body)
    return {"ok": True}


def _schedule_run(request):
    run_job_now(request.body["name"])
    return {"ok": True}


ROUTES = {
    ("POST", "/api/schedules"): ("operator", _schedule_saved),
    ("POST", "/api/schedules/run"): ("operator", _schedule_run),
    ("POST", "/api/images/prepull"): ("operator", _prepull),
    ("POST", "/api/images/cleanup"): ("admin", _cleanup_image),
    ("POST", "/api/images/vm/delete"): ("admin", _delete_vm_image),
    ("POST", "/api/images/forget-rollback"): ("admin", lambda request: forget_rollback(
        request.body.get("namespace") or NS, request.body.get("name", ""))),
    ("POST", "/api/imports/delete"): ("admin", _delete_import),
    ("POST", "/api/import/preview"): ("admin", lambda request: preview_import(request.body)),
    ("POST", "/api/import"): ("admin", lambda request: reviewed_import(request.body)),
    ("GET", "/api/vm-disks"): ("viewer", lambda request: list_vm_disks()),
    ("GET", "/api/sources"): ("admin", lambda request: list_sources()),
    ("GET", "/api/imports"): ("viewer", lambda request: import_status()),
    ("POST", "/api/imports/cleanup-plan"): ("admin", lambda request: import_cleanup_plan(request.body.get("name"), request.body.get("namespace"))),
    ("POST", "/api/images/prepull/stop"): ("operator", lambda request: stop_prepull(request.body.get("name"))),
    ("POST", "/api/images/scan"): ("admin", lambda request: start_image_scan()),
    ("POST", "/api/schedules/delete"): ("operator", lambda request: del_job(request.body["name"])),
    ("POST", "/api/sources/scan"): ("admin", lambda request: scan_source(request.body.get("name"), request.user)),
    ("POST", "/api/sources/trust"): ("admin", lambda request: trust_source(request.body, request.user)),
    ("POST", "/api/sources/inspect"): ("admin", lambda request: inspect_source_container(request.body["name"], request.body["container"])),
    ("GET", "/api/images/vm"): ("viewer", lambda request: homestead_routes.cached("vmimages", 15, vm_image_cache)),
    ("GET", "/api/vmimages"): ("viewer", lambda request: homestead_routes.cached("vmimg", 30, list_vm_images)),
    ("GET", "/api/images"): ("viewer", lambda request: homestead_routes.cached("imgcache", 30, image_cache)),
    ("GET", "/api/schedules"): ("viewer", lambda request: homestead_routes.cached("cron", 8, list_jobs)),
    ("POST", "/api/sources/measure"): ("admin", lambda request: measure_source_paths(request.body.get("name"), request.body.get("paths") or [], request.body.get("seconds", 25))),
    ("POST", "/api/sources"): ("admin", lambda request: {"ok": True, "sources": add_source(request.body["name"], request.body["host"], request.body["user"], request.body.get("password"), request.body.get("kind", "unraid"), request.body.get("base_path", "/mnt/user/appdata"), request.body.get("port", 22))}),
    ("POST", "/api/sources/delete"): ("admin", lambda request: {"ok": True, "sources": del_source(request.body["name"])}),
    ("POST", "/api/sources/browse"): ("admin", lambda request: {"entries": browse_source(request.body["name"], request.body.get("path"))}),
    ("POST", "/api/sources/containers"): ("admin", lambda request: {"containers": source_containers(request.body["name"])}),
    ("GET", "/api/volumes/ownership"): ("viewer", lambda request: ownership_hint(
        (request.query.get("namespace") or [NS])[0], (request.query.get("name") or [""])[0])),
    ("GET", "/api/vm-disks/import-plan"): ("viewer", lambda request: vm_disk_import_plan(
        (request.query.get("ns") or [NS])[0], (request.query.get("name") or [""])[0])),
    ("POST", "/api/volumes/chown"): ("admin", lambda request: chown_claim(
        request.body.get("namespace") or NS, request.body.get("name"), request.body.get("uid"), request.body.get("gid"))),
    ("POST", "/api/vm-disks/import"): ("admin", lambda request: import_vm_disk(request.body, ops=OPS)),
}
