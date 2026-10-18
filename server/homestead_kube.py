"""Homestead's Kubernetes client: the reads and writes everything goes
through, and the API helpers that lived in server.py (#594 moved them here as
homestead_kube_helpers; #619 grew it into this client).

Reads: get, get_optional (None when the object is not there), items (a whole
list or an error). Writes: send and delete_if_present, which go round
WRITE_GUARDS in order before the transport _ksend. kget and ksend stay as the
old names; the new functions call them, so a test that patches kget or ksend
here reaches the new functions too. Also here: who may write to a workload,
raw and table reads, path checks, log search reads, and CPU, memory and age
parsing.

server.py binds what it still owns through bind_routes(), the same entry
point every moved module has whether or not it declares ROUTES: each helper
in ROUTE_NEEDS is looked up in server.py when it is called; each name in
VALUES is server.py's own value. Tests patch kget and ksend here (server.py
imports this module as KUBE_HELPERS)."""
import json
import re
import time
import urllib.request, urllib.parse, urllib.error
import homestead_auth as AUTH
import homestead_changes as CHANGES
import homestead_host_access as HOSTACCESS
import homestead_names as NAMES
import homestead_operations as OPS
import homestead_self as SELF
import homestead_storage_guard as STORAGE_GUARD

ROUTE_NEEDS = ("cluster_shutdown", "require_self_data_write", "self_data_activity")
VALUES = ("API", "CTX", "TOKEN")
KGET_RETRY = (0.5, 1.0, 2.0)  # a busy API server (priority and fairness) answers reads 429 for a moment
# Set once server.py has imported what the cluster shutdown fence reads: a
# write before then (while server.py is still importing) is not fenced.
FENCE_READY = False


def bind_routes(helpers, **values):
    missing = set(ROUTE_NEEDS) - set(helpers)
    if missing:
        raise TypeError("homestead_kube needs " + ", ".join(sorted(missing)))
    if set(values) != set(VALUES):
        raise TypeError("homestead_kube is bound with " + ", ".join(sorted(set(values) ^ set(VALUES))))
    globals().update({name: helpers[name] for name in ROUTE_NEEDS})
    globals().update(values)


# A dot segment, plain or percent-encoded, anywhere in a path.
_DOT_SEGMENT = re.compile(r"(^|/)(\.|%2e){1,2}(/|$)", re.I)


def api_path(path):
    """A Kubernetes API path, refused if a name in it could step out of the
    object it names. Names come from requests - ?name=, a body - and the API
    server is asked as Homestead, so "../" in one must never reach it."""
    head = str(path).split("?", 1)[0]
    if (_DOT_SEGMENT.search(head) or "%2f" in head.lower() or "%5c" in head.lower()
            or any(c in head for c in ("\\", " ", "\t", "\r", "\n", "#"))):
        raise ValueError("that is not a valid Kubernetes name")
    return path


# Each write guard is guard(method, path, body, proceed): it checks or records
# the write and returns proceed(), or wraps it, or raises to stop the write.
# They run in this order around every write; the last one hands over to the
# transport.
def self_data_fence(method, path, body, proceed):
    # Holds Homestead's own data still while the write runs, and refuses
    # writes while that data is being moved.
    with self_data_activity():
        require_self_data_write()
        return proceed()


def workload_access(method, path, body, proceed):
    authorize_workload_write(method, path, body)
    return proceed()


def change_note(method, path, body, proceed):
    CHANGES.note_write(method, path, body)
    return proceed()


def storage_guard(method, path, body, proceed):
    return STORAGE_GUARD.send(method, path, body, proceed, OPS, kget, own_controller=(SELF.NS, NAMES.BRAND))


WRITE_GUARDS = [self_data_fence, workload_access, change_note, storage_guard]


def add_write_guard(guard, *, before=None):
    """Register a write guard: last, or just before the guard named before."""
    if before is None:
        WRITE_GUARDS.append(guard)
        return
    names = [existing.__name__ for existing in WRITE_GUARDS]
    if before not in names:
        raise ValueError(f"no write guard named {before}")
    WRITE_GUARDS.insert(names.index(before), guard)


def run_write_guards(method, path, body, dispatch):
    guards = tuple(WRITE_GUARDS)

    def step(index):
        if index == len(guards):
            return dispatch()
        return guards[index](method, path, body, lambda: step(index + 1))
    return step(0)


def _guarded_ksend(method, path, body=None, ctype="application/json", timeout=15, *, shutdown_bypass=False):
    return run_write_guards(method, path, body,
                            lambda: _ksend(method, path, body, ctype, timeout, shutdown_bypass=shutdown_bypass))


def _auth_ksend(method, path, body=None, ctype="application/json", timeout=15):
    # Only AUTH receives this binding. Login rate limits and session revocation
    # must remain durable while workloads are fenced for shutdown/recovery.
    base = f"/api/v1/namespaces/{AUTH.NS}/secrets"
    name = AUTH.SECRET_NAME()
    metadata = body.get("metadata", {}) if isinstance(body, dict) else {}
    if (method not in ("POST", "PUT") or path != (base if method == "POST" else base + "/" + name)
            or not isinstance(body, dict) or body.get("apiVersion") != "v1" or body.get("kind") != "Secret"
            or body.get("type") != "Opaque" or not isinstance(metadata, dict)
            or metadata.get("name") != name or metadata.get("namespace") != AUTH.NS
            or not isinstance(body.get("data"), dict) or set(body["data"]) != {"store.json"}
            or "stringData" in body or ctype != "application/json"):
        raise ValueError("Authentication transport only writes the account Secret")
    return _guarded_ksend(method, path, body, ctype, timeout, shutdown_bypass=True)


def authorize_workload_write(method, path, body):
    if HOSTACCESS.role() in (None, "admin"):
        return
    match = re.fullmatch(r"(/(?:api/v1|apis/apps/v1|apis/batch/v1)/namespaces/([^/]+)/(deployments|statefulsets|daemonsets|pods|jobs))(?:/([^/?]+)(?:/[^?]+)?)?", path.split("?", 1)[0])
    if not match:
        return
    base, ns, _, name = match.groups()
    if name:
        HOSTACCESS.require_target(kget(base + "/" + name), ns)
    if isinstance(body, dict) and (body.get("spec", {}).get("template") or method == "POST"):
        HOSTACCESS.require_target(body, ns)


def age_secs(ts):
    if not ts:
        return 0
    try:
        t = time.strptime(ts.replace("Z", "GMT"), "%Y-%m-%dT%H:%M:%S%Z")
        import calendar
        return max(0, int(time.time() - calendar.timegm(t)))
    except Exception:
        return 0


def parse_cpu(s):
    if not s: return 0.0
    s = str(s)
    if s.endswith("n"): return float(s[:-1]) / 1e9
    if s.endswith("u"): return float(s[:-1]) / 1e6
    if s.endswith("m"): return float(s[:-1]) / 1e3
    try: return float(s)
    except: return 0.0


def parse_mem(s):
    if not s: return 0
    s = str(s)
    mult = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
            "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}
    for suf, m in mult.items():
        if s.endswith(suf):
            try: return int(float(s[:-len(suf)]) * m)
            except: return 0
    try: return int(float(s))
    except: return 0


def logsearch_read(ns, pod, container, since, limit):
    """One container's log lines of the last since seconds, with timestamps."""
    query = f"sinceSeconds={int(since)}&limitBytes={int(limit)}&timestamps=true" + (
        f"&container={urllib.parse.quote(container)}" if container else "")
    req = urllib.request.Request(f"{API}/api/v1/namespaces/{urllib.parse.quote(ns)}/pods/{urllib.parse.quote(pod)}/log?{query}",
                                 headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, context=CTX, timeout=20) as r:
        return r.read(int(limit) + 1).decode("utf-8", "replace")


def _never_started(pod):
    """Nothing ever ran in it: every container still waiting, or none reported."""
    statuses = (pod.get("status") or {}).get("containerStatuses") or []
    return all(not (cs.get("state") or {}).get("running") and not (cs.get("state") or {}).get("terminated")
               and not (cs.get("lastState") or {}).get("terminated") for cs in statuses)


def raw_get(path, timeout=20):
    """Plain-text GET against the API (pod logs and similar)."""
    req = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, context=CTX, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def ktable(path, timeout=20):
    """A list as the API server prints it: the columns kubectl get shows."""
    req = urllib.request.Request(API + path, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json;as=Table;v=v1;g=meta.k8s.io,application/json"})
    with urllib.request.urlopen(req, context=CTX, timeout=timeout) as r:
        return json.loads(r.read().decode())


def kget(path, timeout=10):
    for attempt in range(len(KGET_RETRY) + 1):
        req = urllib.request.Request(API + api_path(path), headers={"Authorization": f"Bearer {TOKEN}"})
        try:
            with urllib.request.urlopen(req, context=CTX, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as error:
            # A read is safe to ask again; a write's 429 (an eviction a
            # disruption budget refuses) is an answer, and ksend keeps it.
            if error.code != 429 or attempt == len(KGET_RETRY):
                raise
            try:
                delay = float((error.headers or {}).get("Retry-After") or KGET_RETRY[attempt])
            except (TypeError, ValueError):
                delay = KGET_RETRY[attempt]
            error.close()
            time.sleep(max(0.1, min(3.0, delay)))


def ksend(method, path, body=None, ctype="application/json", timeout=15):
    return _guarded_ksend(method, path, body, ctype, timeout)


def get(path, **options):
    return kget(path, **options)


def get_optional(path, **options):
    """The object, or None when it is not there (404). Any other failure
    raises: a read that failed is not an object that is missing."""
    try:
        return kget(path, **options)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        error.close()
        return None


def items(path, optional_group=None):
    """Every row of a list, or an error: never a list that failed, came back
    without items, or was cut short (continue). A 429 is retried by kget.
    optional_group names an API group that may not be installed (say
    snapshot.storage.k8s.io): a 404 is then an empty list if API discovery
    confirms the group is absent, and an error if the group is there."""
    try:
        listing = kget(path)
    except urllib.error.HTTPError as error:
        if error.code != 404 or not optional_group:
            raise
        groups = kget("/apis").get("groups")
        if not isinstance(groups, list) or any(not isinstance(g, dict) or not g.get("name") for g in groups):
            raise ValueError("Kubernetes did not say which APIs it serves; refresh and try again")
        if any(g["name"] == optional_group for g in groups):
            raise
        error.close()
        return []
    if (not isinstance(listing, dict) or not isinstance(listing.get("items"), list)
            or (listing.get("metadata") or {}).get("continue")):
        raise ValueError(f"Kubernetes returned an incomplete list for {path}; refresh and try again")
    if any(not isinstance(row, dict) for row in listing["items"]):
        raise ValueError(f"Kubernetes returned a list with invalid entries for {path}; refresh and try again")
    return listing["items"]


def send(method, path, body=None, **options):
    return ksend(method, path, body, **options)


def delete_if_present(path, body=None, **options):
    """Delete it; None when it was already gone (404). Any other failure raises."""
    try:
        return ksend("DELETE", path, body, **options)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        error.close()
        return None


def require_workload_target(ns, name):
    if HOSTACCESS.role() not in (None, "admin"):
        HOSTACCESS.require_target(kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}"), ns)


def _ksend(method, path, body=None, ctype="application/json", timeout=15, *, shutdown_bypass=False):
    path = api_path(path)
    # Fence the common Kubernetes transport, including callers that already
    # own another feature's write lock. Local navigation and recovery reviews
    # remain available. The shutdown coordinator and the narrowly scoped AUTH
    # binding bypass this fence; authentication still uses the other guards.
    if not shutdown_bypass and FENCE_READY:
        shutdown = cluster_shutdown().state()
        if shutdown and shutdown["phase"] not in ("released", "failed"):
            raise ValueError("Cluster shutdown is active; open Cluster → Shut down cluster for progress or recovery")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": ctype})
    with urllib.request.urlopen(req, context=CTX, timeout=timeout) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw.strip() else {}
