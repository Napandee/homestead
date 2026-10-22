"""Starts after (#746): an app names the apps it starts after, and every new
pod of it waits for their Services before anything of it runs.

The setting lives on the Deployment, as the annotation KEY. From it one
function renders the start gate: a small init container, always first, that
waits until each dependency's Service answers on its TCP port (or until the
timeout) and then exits. It reads no Kubernetes API, so it works on a cold
boot with Homestead still down: a ClusterIP Service only routes to Ready
pods, so a Service that answers is a dependency that is ready.

Homestead and system apps are never gated, so this can't hold up the
platform's own recovery. An app Flux or Argo CD applies is never written:
Homestead shows the lines to commit instead, as it does for tiers.

Nothing here is bound by server.py: it reads through the helpers
homestead_workloads and homestead_lifecycle already have.
"""
import json

import homestead_names as NAMES
import homestead_namespaces as NAMESPACES

KEY = NAMES.key("starts-after")
GATE = f"{NAMES.BRAND}-start-gate"
TIMEOUT = (30, 300, 3600)        # seconds: least, default, most
MAX_AFTER = 10
ON_TIMEOUT = ("start", "wait")   # the first is the default
NO_SERVICE = "{app} has no Service with a TCP port, so nothing can tell when it is ready"


def _workloads():
    import homestead_workloads as WORKLOADS
    return WORKLOADS


def _meta(obj):
    return obj.get("metadata") or {}


def _ref(dep):
    meta = _meta(dep)
    return meta.get("namespace", ""), meta.get("name", "")


def _pod_spec(dep):
    return ((dep.get("spec") or {}).get("template") or {}).get("spec") or {}


def _dns_label(value, label):
    import homestead_lifecycle as LC
    return LC.dns_label(value if isinstance(value, str) else "", label)


def never_gated(ns, name):
    """Homestead itself, the helpers it runs (its SMB and NFS servers, the
    object store), and anything in a system namespace: the platform's own
    recovery must never wait for an app."""
    part = getattr(_workloads(), "homestead_part", None)
    return (_is_self(ns, name) or bool(part and part(ns, name)) or ns in (_workloads().SYS_NS or ())
            or NAMESPACES.is_system({"metadata": {"name": ns}}))


def read(dep):
    """The stored setting; None when there is none, {"unreadable": True}
    when it isn't what Homestead writes, so a row shows it and never fails."""
    raw = (_meta(dep).get("annotations") or {}).get(KEY)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {"unreadable": True}
    if not isinstance(value, dict) or not isinstance(value.get("after"), list):
        return {"unreadable": True}
    return value


def after_of(dep):
    """(ns, app) of each app a Deployment's setting names, in its order."""
    setting = read(dep) or {}
    return [(str(a.get("ns") or ""), str(a.get("app") or "")) for a in setting.get("after") or []
            if isinstance(a, dict)]


def services_for(ns, app, deployments, services):
    """The Services that select the app's pods and can show it is ready:
    [{"name", "ports": [TCP ports]}]. An ExternalName Service, one with no
    selector, or one that publishes pods before they are ready would answer
    at once, so they don't count. A headless Service resolves to the pods
    themselves, so only a port that is its own target counts there."""
    dep = next((d for d in deployments if _ref(d) == (ns, app)), None)
    if dep is None:
        return []
    labels = ((((dep.get("spec") or {}).get("template") or {}).get("metadata") or {}).get("labels") or {})
    found = []
    for svc in services:
        if _meta(svc).get("namespace") != ns:
            continue
        spec = svc.get("spec") or {}
        selector = spec.get("selector") or {}
        if (spec.get("type") == "ExternalName" or not selector or spec.get("publishNotReadyAddresses")
                or any(labels.get(k) != v for k, v in selector.items())):
            continue
        headless = spec.get("clusterIP") == "None"
        ports = [p["port"] for p in spec.get("ports") or []
                 if isinstance(p.get("port"), int) and str(p.get("protocol") or "TCP").upper() == "TCP"
                 and (not headless or p.get("targetPort") in (None, p["port"]))]
        if ports:
            found.append({"name": _meta(svc).get("name", ""), "ports": ports})
    return found


def graph(deployments):
    """(ns, app) -> [(ns, app), ...] from every Deployment's setting."""
    edges = {}
    for dep in deployments:
        after = after_of(dep)
        if after:
            edges[_ref(dep)] = after
    return edges


def find_cycle(edges, start):
    """The app names from start round to start, or None."""
    path, on_path, seen = [start], {start}, set()

    def walk(node):
        for nxt in edges.get(node, ()):
            if nxt == start:
                return path + [start]
            if nxt in on_path or nxt in seen:
                continue
            path.append(nxt)
            on_path.add(nxt)
            found = walk(nxt)
            if found:
                return found
            path.pop()
            on_path.discard(nxt)
            seen.add(nxt)
        return None

    found = walk(start)
    return [name for _, name in found] if found else None


def clean(dep, value, deployments, services):
    """The setting as it is stored, or None to remove it. Refused (ValueError)
    when it can't work: a loop, Homestead or a system app as the one waiting,
    a dependency nothing can see is ready, limits out of range."""
    if value is None:
        return None
    ns, name = _ref(dep)
    if isinstance(value, dict) and value.get("unreadable"):
        raise ValueError(f"Starts after can't be read on {name}; set it again or clear it")
    if not isinstance(value, dict) or not isinstance(value.get("after", []), list):
        raise ValueError("Starts after must be a list of apps")
    timeout = value.get("timeout", TIMEOUT[1])
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not TIMEOUT[0] <= timeout <= TIMEOUT[2]:
        raise ValueError("wait up to must be between 30 s and 60 min")
    on_timeout = value.get("on_timeout", ON_TIMEOUT[0])
    if on_timeout not in ON_TIMEOUT:
        raise ValueError("if it never comes up: start or wait")
    wanted, seen = [], set()
    for item in value.get("after") or []:
        if not isinstance(item, dict):
            raise ValueError("Starts after must be a list of apps")
        ref = (_dns_label(item.get("ns") or ns, "namespace"), _dns_label(item.get("app"), "app name"))
        if ref not in seen:
            seen.add(ref)
            wanted.append((ref, item))
    if not wanted:
        return None
    if len(wanted) > MAX_AFTER:
        raise ValueError(f"Starts after takes at most {MAX_AFTER} apps")
    if never_gated(ns, name):
        raise ValueError("Homestead and system apps can't wait for other apps")
    # Only what changed is checked: an entry kept exactly as stored stays,
    # even when its app has since gone (the alerts say so), so an unrelated
    # edit never fails on it. A new or changed entry must work now.
    kept = (read(dep) or {}).get("after") or []
    after = []
    for (dep_ns, app), item in wanted:
        if (dep_ns, app) == (ns, name):
            raise ValueError("an app can't start after itself")
        as_stored = {"ns": dep_ns, "app": app, "service": item.get("service"), "port": item.get("port")}
        if as_stored in kept:
            after.append(as_stored)
            continue
        found = services_for(dep_ns, app, deployments, services)
        if item.get("service") not in (None, ""):
            chosen = _dns_label(item["service"], "Service name")
            found = [s for s in found if s["name"] == chosen]
        if not found:
            raise ValueError(NO_SERVICE.format(app=app))
        svc = found[0]
        port = item.get("port")
        if port in (None, ""):
            port = svc["ports"][0]
        elif isinstance(port, bool) or not isinstance(port, int) or port not in svc["ports"]:
            raise ValueError(f"{app}'s Service has no port {port}")
        after.append({"ns": dep_ns, "app": app, "service": svc["name"], "port": port})
    spec = _pod_spec(dep)
    if spec.get("dnsPolicy") in ("Default", "None"):
        raise ValueError(f"{name} does not use cluster DNS (dnsPolicy {spec['dnsPolicy']}), so it can't find "
                         f"{after[0]['app']}; set dnsPolicy to ClusterFirst")
    if spec.get("hostNetwork") and spec.get("dnsPolicy") != "ClusterFirstWithHostNet":
        raise ValueError(f"{name} uses the host's network without cluster DNS; set dnsPolicy to "
                         f"ClusterFirstWithHostNet so it can find {after[0]['app']}")
    edges = graph(deployments)
    edges[(ns, name)] = [(a["ns"], a["app"]) for a in after]
    loop = find_cycle(edges, (ns, name))
    if loop:
        raise ValueError("a cycle: " + " → ".join(loop))
    return {"after": after, "timeout": timeout, "on_timeout": on_timeout}


# ---- the start gate ---------------------------------------------------------
# alpine 3.24.3 (busybox 1.37.0 nc and sh), by its multi-arch index digest,
# resolved 2026-10-22. The same repository Homestead already uses
# (homestead_volowner.IMAGE), so an air-gapped mirror has it. Changing it
# changes every gate, and so restarts every gated app once: bump it only
# with a reason (a fix to busybox nc or sh).
GATE_IMAGE = ("docker.io/library/alpine:3.24"
              "@sha256:71e9b7bae160ba167fa17414401b94cbcdaf3c32feeab9298bd794a2a0614c35")
# Fixed text: names reach it only through env (HS_AFTER, HS_TIMEOUT,
# HS_ON_TIMEOUT), so nothing a person types becomes shell. It never exits
# non-zero, which would put the pod in Init:CrashLoopBackOff and delay its
# start once the dependency is ready. HS_MESSAGE exists for the tests.
SCRIPT = """start=$(date +%s); last=; said=0
while :; do
  missing=
  for t in $HS_AFTER; do nc -z -w 2 "${t%:*}" "${t##*:}" >/dev/null 2>&1 || missing="$missing $t"; done
  now=$(date +%s); took=$((now - start))
  if [ -z "$missing" ]; then echo "ready after $took s"; exit 0; fi
  if [ "$missing" != "$last" ] || [ $((now - said)) -ge 60 ]; then
    echo "waiting for$missing ($took s of $HS_TIMEOUT s)"; last=$missing; said=$now; fi
  if [ "$HS_ON_TIMEOUT" = start ] && [ "$took" -ge "$HS_TIMEOUT" ]; then
    echo "timeout$missing" > "${HS_MESSAGE:-/dev/termination-log}"
    echo "not ready after $took s: starting anyway"; exit 0; fi
  sleep 2
done
"""


def target(item):
    """Where the gate knocks for one dependency: <service>.<ns>.svc:<port>."""
    return f"{item['service']}.{item['ns']}.svc:{item['port']}"


def render(setting):
    """The gate container for a stored setting."""
    return {"name": GATE, "image": GATE_IMAGE, "imagePullPolicy": "IfNotPresent",
            "command": ["sh", "-c", SCRIPT],
            "env": [{"name": "HS_AFTER", "value": " ".join(target(a) for a in setting["after"])},
                    {"name": "HS_TIMEOUT", "value": str(setting["timeout"])},
                    {"name": "HS_ON_TIMEOUT", "value": setting["on_timeout"]}],
            "resources": {"requests": {"cpu": "5m", "memory": "8Mi"}, "limits": {"cpu": "50m", "memory": "16Mi"}},
            "securityContext": {"runAsNonRoot": True, "runAsUser": 65534, "runAsGroup": 65534,
                                "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}},
            "terminationMessagePolicy": "File"}


def _matches(have, want):
    """have is want, give or take what the API server fills in when it
    stores a template (terminationMessagePath and the like)."""
    return all(have.get(key) == value for key, value in want.items())


def _stored_text(setting):
    return json.dumps(setting, sort_keys=True, separators=(",", ":"))


def apply_to(dep, setting):
    """Write the setting and its gate, first in initContainers; None takes
    both away and touches nothing else. A gate that already matches is kept
    as it is, so saving the same setting again restarts nothing."""
    spec = dep["spec"]["template"]["spec"]
    current = spec.get("initContainers") or []
    others = [c for c in current if c.get("name") != GATE]
    if setting is None:
        annotations = _meta(dep).get("annotations")
        if annotations:
            annotations.pop(KEY, None)
        if others:
            spec["initContainers"] = others
        elif "initContainers" in spec and len(others) != len(current):
            spec.pop("initContainers")
        return
    dep["metadata"].setdefault("annotations", {})[KEY] = _stored_text(setting)
    want = render(setting)
    gate = current[0] if current and current[0].get("name") == GATE and _matches(current[0], want) else want
    spec["initContainers"] = [gate] + others


def gate_state(dep):
    """"none", "rendered", "missing" (set, no gate) or "differs" (a gate
    changed, moved or left with no setting)."""
    setting = read(dep)
    current = _pod_spec(dep).get("initContainers") or []
    gates = [i for i, c in enumerate(current) if c.get("name") == GATE]
    if setting is None:
        return "differs" if gates else "none"
    if setting.get("unreadable"):
        return "differs"
    if not gates:
        return "missing"
    try:
        want = render(setting)
    except (KeyError, TypeError):
        return "differs"
    return "rendered" if gates == [0] and _matches(current[0], want) else "differs"


# ---- saving it ---------------------------------------------------------------

def lists(get):
    """Every Deployment and Service, which the checks need."""
    return (get("/apis/apps/v1/deployments").get("items", []) or [],
            get("/api/v1/services").get("items", []) or [])


def _managed(dep):
    import homestead_updates as UPDATES
    return UPDATES.managed_by(dep)


def managed_message(name, owner):
    return f"{name}'s start order is set where {owner['by']} applies it; commit the lines Homestead shows"


def _same(dep, setting):
    stored = read(dep)
    if stored is not None and stored.get("unreadable"):
        return False
    return stored == setting


def unchanged(dep, value):
    """Whether value is what Edit's form showed for dep and nothing more: the
    stored setting itself, or the marker of one that can't be read. Edit sends
    the form back whole, so that is no change, and nothing of it is checked."""
    if isinstance(value, dict) and value.get("unreadable"):
        return True
    stored = read(dep)
    return not (stored or {}).get("unreadable") and value == stored


def apply(dep, value, deployments, services):
    """The setting checked and written onto dep (a copy being edited). An app
    Flux or Argo CD applies is never written: a change is refused, and the
    same setting again is left alone."""
    if unchanged(dep, value):
        return
    setting = clean(dep, value, deployments, services)
    owner = _managed(dep)
    if owner:
        if not _same(dep, setting):
            raise ValueError(managed_message(_ref(dep)[1], owner))
        return
    apply_to(dep, setting)


def changes(dep, value, get):
    """Whether saving value would change dep's setting (checked first)."""
    return not _same(dep, clean(dep, value, *lists(get)))


def _yaml(value):
    # JSON is YAML: every value is written as a JSON scalar or list.
    return json.dumps(value)


def git_lines(dep, setting):
    """What to commit for an app Flux or Argo CD applies: the annotation and
    the gate, first in initContainers. For a HelmRelease, the same as chart
    values."""
    helm = str((_managed(dep) or {}).get("source") or "").startswith("HelmRelease")
    head = ("# chart values, if the chart takes extra initContainers and podAnnotations;\n"
            "# if it doesn't, add them with a Flux post-renderer patch\n") if helm else ""
    if setting is None:
        return head + (f"# remove {KEY} from metadata.annotations and the {GATE} entry from "
                       "spec.template.spec.initContainers\n")
    gate = render(setting)
    item = [f"- name: {gate['name']}"] + [f"  {key}: {_yaml(value)}" for key, value in gate.items() if key != "name"]
    annotation = f"{KEY}: {_yaml(_stored_text(setting))}"
    if helm:
        return head + f"podAnnotations:\n  {annotation}\ninitContainers:   # first\n" + \
            "".join(f"  {line}\n" for line in item)
    return (f"metadata:\n  annotations:\n    {annotation}\n"
            "spec:\n  template:\n    spec:\n      initContainers:   # first, before any others\n"
            + "".join(f"        {line}\n" for line in item))


def _image_wanted(deployments, get):
    """True until a first gate is written: then no node has the image yet,
    and a cold boot with the internet down could not pull it."""
    if any(c.get("name") == GATE for d in deployments for c in _pod_spec(d).get("initContainers") or []):
        return False
    digest = GATE_IMAGE.split("@", 1)[1]
    for node in get("/api/v1/nodes").get("items", []) or []:
        for image in (node.get("status") or {}).get("images") or []:
            if any(name.endswith("@" + digest) for name in image.get("names") or []):
                return False
    return True


def ensure_image(deployments, get):
    """Pre-pull the gate's image to every node the first time a gate is
    written (deployments: as they were before). A note when that fails;
    the save itself stands."""
    import homestead_image_cache as IMAGES
    try:
        if _image_wanted(deployments, get):
            IMAGES.prepull(GATE_IMAGE)
    except Exception as error:
        return f"the start gate's image could not be pre-pulled: {error}"
    return None


def _setting_of(dep):
    setting = read(dep)
    return None if setting is None or setting.get("unreadable") else setting


def form(ns, name):
    """Edit's Starts after: the setting, who applies the app, the gate as it
    is, the apps it may start after, and for a managed app the lines to
    commit."""
    import homestead_lifecycle as LC
    ns, name = LC.dns_label(ns, "namespace"), LC.dns_label(name, "app name")
    dep = LC.kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    deployments, services = lists(LC.kget)
    setting = _setting_of(dep)
    owner = _managed(dep)
    gate = gate_state(dep)
    if owner and gate == "missing":
        gate = "not-committed"
    candidates = [{"ns": d_ns, "app": d_name, "services": services_for(d_ns, d_name, deployments, services)}
                  for d_ns, d_name in sorted(_ref(d) for d in deployments)
                  if (d_ns, d_name) != (ns, name) and not (never_gated(d_ns, d_name) and not _is_self(d_ns, d_name))]
    return {"after": (setting or {}).get("after", []),
            "timeout": (setting or {}).get("timeout", TIMEOUT[1]),
            "on_timeout": (setting or {}).get("on_timeout", ON_TIMEOUT[0]),
            "unreadable": bool((read(dep) or {}).get("unreadable")),
            "managed_by": (owner or {}).get("by"), "gate": gate, "candidates": candidates,
            "git": git_lines(dep, setting) if owner else ""}


def _is_self(ns, name):
    is_self = getattr(_workloads(), "is_self", None)
    return bool(is_self and is_self(ns, name))


def save(body):
    """Starts after saved on its own: {"ok", "restarts"}, and a note when
    the gate's image could not be pre-pulled. A managed app gets 409 with
    the lines to commit, and nothing is written."""
    import homestead_lifecycle as LC
    import homestead_routes as ROUTER
    ns, name = LC.dns_label(body.get("ns"), "namespace"), LC.dns_label(body.get("name"), "app name")
    # The checks Edit's save makes first (homestead_workloads.edit): never
    # Homestead's SMB or NFS server, and Homestead itself only as Edit allows.
    WORKLOADS = _workloads()
    WORKLOADS.guard_managed_smb(ns, name)
    WORKLOADS.guard_self(ns, name)
    dep = LC.kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    deployments, services = lists(LC.kget)
    value = {key: body[key] for key in ("after", "timeout", "on_timeout") if key in body}
    setting = clean(dep, value, deployments, services)
    owner = _managed(dep)
    if owner:
        return ROUTER.Reply(409, {"error": managed_message(name, owner), "git": git_lines(dep, setting)})
    cfg = {"ns": ns, "name": name, "starts_after": value}
    prepared = LC.prepare_edit(cfg, dep)
    restarts = LC.restarts(dep, prepared["deployment"])
    if prepared["deployment"] != dep:
        LC.edit_workload(cfg, prepared=prepared)
    note = ensure_image(deployments, LC.kget) if setting else None
    return {"ok": True, "restarts": restarts, **({"note": note} if note else {})}


def _arg(request, key):
    return (request.query.get(key) or [""])[0]


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/workloads/starts-after"): ("viewer", lambda request: form(_arg(request, "ns"), _arg(request, "name"))),
    ("POST", "/api/workloads/starts-after"): ("operator", lambda request: save(request.body)),
}
