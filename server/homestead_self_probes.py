"""Homestead's own health checks, compared with this release's (#1561).

The in-app update changes Homestead's image and nothing else
(homestead_update_rollouts), so an install keeps the readiness, liveness and
startup probes it was installed with: one installed before 2.9.0 still asks
/healthz, and #1263's data check is never asked. This compares the probes of
Homestead's own container with those of the manifest the image carries (the
one homestead_self reads), and Settings shows the result beside Permissions
with the fix that suits how Homestead was installed. It only reads: Homestead
never changes its own pod template here.

The comparison is on what the kubelet does. Only the fields this release's
manifest sets are compared, and a field the API server fills in when the
manifest leaves it out (failureThreshold, the httpGet scheme ...) is taken at
its default, so it is never a difference. A named port is the container's
port of that name.
"""
import copy
import json
import re
import shlex
import urllib.error

import homestead_kube as KUBE
import homestead_names as NAMES
import homestead_self as SELF
import homestead_updates as UPDATES
import homestead_yaml as YAML

PROBES = (("readinessProbe", "readiness"), ("livenessProbe", "liveness"), ("startupProbe", "startup"))
HANDLERS = {"httpGet": "sends an HTTP request", "tcpSocket": "opens a connection",
            "exec": "runs a command", "grpc": "makes a gRPC call"}
# What the API server sets when a probe leaves the field out.
DEFAULTS = {"timeoutSeconds": 1, "periodSeconds": 10, "successThreshold": 1, "failureThreshold": 3,
            "initialDelaySeconds": 0, "httpGet.scheme": "HTTP"}
# What a newer check changes for the person, said once beside the fix.
EFFECTS = {("readinessProbe", "/readyz?check=data"):
           "With this release's check, a copy of Homestead that cannot write its data stops taking requests, "
           "even when that is every copy."}
CHART = "oci://ghcr.io/homestead-lab/charts/homestead"
# What may go into a command a person copies into a shell. Labels and
# annotations are free text anyone who may edit the Deployment can write: a
# value that is not a plain name gives no command, never another name's.
NAME = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")      # a DNS-1123 label
RELEASE_MAX, NAMESPACE_MAX = 53, 63
VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z]+(\.[0-9A-Za-z]+)*)?", re.ASCII)
TEXT_MAX = 120


def _plain(value, limit=TEXT_MAX):
    """Cluster text shown to a person: one line, no control characters, capped."""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value)).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _name(value, limit):
    return isinstance(value, str) and len(value) <= limit and bool(NAME.fullmatch(value))


def _version(value):
    return isinstance(value, str) and bool(VERSION_PATTERN.fullmatch(value))


def _no_command(what):
    return f"{what} is not a usable name, so the command cannot be given."


def _unknown(detail):
    return {"state": "unknown", "detail": detail, "differences": []}


def _container(dep):
    for container in (((dep or {}).get("spec") or {}).get("template") or {}).get("spec", {}).get("containers") or []:
        if isinstance(container, dict) and container.get("name") == NAMES.BRAND:
            return container
    return None


def _wanted():
    """This release's Deployment for Homestead, from the manifest in the image."""
    for doc in SELF.manifest_documents():
        if isinstance(doc, dict) and doc.get("kind") == "Deployment" \
                and isinstance(doc.get("metadata"), dict) and doc["metadata"].get("name") == NAMES.BRAND:
            return doc
    return None


def _port(value, container):
    """A named port as its number in the container it belongs to."""
    if isinstance(value, str):
        for port in container.get("ports") or []:
            if port.get("name") == value:
                return port.get("containerPort")
    return value


def _path(probe):
    return ((probe or {}).get("httpGet") or {}).get("path")


def _word(value):
    return _plain(json.dumps(value) if isinstance(value, (dict, list)) else value)


def _compare(key, kind, want, have, want_box, have_box):
    """How the live probe differs from this release's, field by field."""
    def row(field, live, wanted, text):
        return {"probe": kind, "field": field, "live": live, "wanted": wanted, "text": text}
    if not want and not have:
        return []
    if not have:
        return [row(key, None, want, f"There is no {kind} check. This release has one.")]
    if not want:
        return [row(key, have, None, f"This release has no {kind} check.")]
    rows = []
    for field, wanted in want.items():
        if field in HANDLERS:
            if field not in have:
                live = next((name for name in HANDLERS if name in have), "")
                rows.append(row(field, live, field, f"The {kind} check {HANDLERS.get(live, 'has no handler')}. "
                                                    f"This release's check {HANDLERS[field]}."))
                continue
            for name, value in (wanted or {}).items():
                live = have[field].get(name, DEFAULTS.get(f"{field}.{name}"))
                if name == "port":
                    same = _port(live, have_box) == _port(value, want_box)
                else:
                    same = live == value
                if same:
                    continue
                text = (f"The {kind} check asks {_word(live)}. This release asks {_word(value)}." if name == "path"
                        else f"The {kind} check has {field}.{name} {_word(live)}. This release sets {_word(value)}.")
                rows.append(row(f"{field}.{name}", live, value, text))
        else:
            live = have.get(field, DEFAULTS.get(field))
            if live != wanted:
                rows.append(row(field, live, wanted,
                                f"The {kind} check has {field} {_word(live)}. This release sets {_word(wanted)}."))
    return rows


def _setting(want, have):
    """What the fix sets for one probe: this release's, and nothing of the old
    one's handler (a probe has one; the API refuses two)."""
    if not want:
        return None
    body = copy.deepcopy(want)
    for name in HANDLERS:
        if name in (have or {}) and name not in body:
            body[name] = None
    return body


def _fix(dep, settings, values):
    """The fix that suits how Homestead was installed."""
    meta = dep.get("metadata") or {}
    labels, notes = meta.get("labels") or {}, meta.get("annotations") or {}
    chart = labels.get("app.kubernetes.io/managed-by") == "Helm"
    version = SELF.VERSION if _version(SELF.VERSION) else ""
    managed = UPDATES.managed_by(dep)
    restart = "Homestead restarts once to use the new checks."
    if managed:
        by = _plain(managed["by"], 60)
        where = f" ({_plain(managed['source'])})" if managed.get("source") else ""
        who = (f"Homestead is updated by {by}{where}, so a change made here is undone at its next sync."
               if managed.get("detected") else f"Homestead is marked as updated elsewhere ({by}).")
        there = f"where {by} reads it" if managed.get("detected") else "there"
        if chart:
            return "managed", {"note": f"{who} Set its chart version to {version or 'this release'} {there}. {restart}"}
        return "managed", {"note": f"{who} Set these checks {there}. {restart}", "values": values}
    if chart:
        release = notes.get("meta.helm.sh/release-name") or labels.get("app.kubernetes.io/instance")
        ns = notes.get("meta.helm.sh/release-namespace") or meta.get("namespace")
        note = ("Homestead was installed with Helm, so a patch is undone at the next upgrade. "
                "Upgrade the release to this version's chart instead. Needs Helm 3.14 or later.")
        refused = ("" if _name(release, RELEASE_MAX) else _no_command("The Helm release name recorded on the Deployment")) \
            or ("" if _name(ns, NAMESPACE_MAX) else _no_command("The Helm namespace recorded on the Deployment")) \
            or ("" if version else "This Homestead's version is not a release version, so the command cannot be given.")
        if refused:
            return "helm", {"note": f"{note} {refused}"}
        # Not --reuse-values: that renders the new chart with the old chart's
        # values.yaml, so a value the chart gained since is missing (2.8.301's
        # samba fails the template) and the old default repository comes back.
        # The tag is set empty: the documented upgrade stored a pinned one, and
        # an empty tag is the chart's own appVersion (_helpers.tpl), so the pin
        # is cleared and later upgrades follow the chart.
        words = ["helm", "upgrade", release, CHART, "-n", ns, "--version", version, "--reset-then-reuse-values",
                 "--set-string", "image.tag=", "--wait", "--timeout", "5m"]
        check = (f"Check first with {shlex.join(['helm', 'get', 'values', release, '-n', ns])}: "
                 "an image repository or other value set there is kept.")
        return "helm", {"note": f"{note} {check} {restart}", "command": shlex.join(words)}
    if not _name(SELF.NS, NAMESPACE_MAX):
        return "manifest", {"note": _no_command("Homestead's namespace")}
    note = ("Run once, wherever kubectl is used. It changes only these checks. "
            f"If another tool applies Homestead, set them there instead. {restart}")
    patch = {"spec": {"template": {"spec": {"containers": [{"name": NAMES.BRAND, **settings}]}}}}
    words = ["kubectl", "-n", SELF.NS, "patch", "deployment", NAMES.BRAND, "-p", json.dumps(patch, separators=(",", ":"))]
    return "manifest", {"note": note, "command": shlex.join(words)}


def report():
    """Whether Homestead's own probes are this release's, and if not, the fix."""
    try:
        want_box = _container(_wanted())
    except (ValueError, TypeError, AttributeError) as error:   # YAML.YamlError is a ValueError
        return _unknown(f"This image's manifest could not be read ({_plain(error, 160)}), "
                        "so there is nothing to compare the checks with.")
    if want_box and any(want_box.get(key) is not None and not isinstance(want_box.get(key), dict)
                        for key, _ in PROBES):
        return _unknown("This image's manifest has a health check that is not written as a map, "
                        "so there is nothing to compare the checks with.")
    if not want_box:
        return _unknown("This image carries no manifest for Homestead, so there is nothing to compare the checks with.")
    path = f"/apis/apps/v1/namespaces/{SELF.NS}/deployments/{NAMES.BRAND}"
    try:
        dep = KUBE.get_optional(path, read=SELF.kget)
    except (urllib.error.URLError, OSError, ValueError, TypeError) as error:
        return _unknown(f"Homestead's Deployment could not be read ({_plain(error, 160)}).")
    if not dep:
        return _unknown(f"There is no Deployment named {NAMES.BRAND} in {_plain(SELF.NS, 63)}, "
                        "so the checks could not be compared.")
    have_box = _container(dep)
    if not have_box:
        return _unknown(f"Homestead's Deployment has no container named {NAMES.BRAND}, "
                        "so the checks could not be compared.")
    differences, settings, values = [], {}, {}
    for key, kind in PROBES:
        rows = _compare(key, kind, want_box.get(key), have_box.get(key), want_box, have_box)
        if rows:
            differences += rows
            settings[key] = _setting(want_box.get(key), have_box.get(key))
            values[key] = copy.deepcopy(want_box.get(key))
    if not differences:
        return {"state": "current", "detail": "They match this release's manifest.", "differences": []}
    install, fix = _fix(dep, settings, YAML.dump(values))
    answer = {"state": "differs", "detail": " ".join(row["text"] for row in differences),
              "differences": differences, "install": install, "fix": fix}
    effects = [EFFECTS[(key, path)] for key, path in ((key, _path(want_box.get(key))) for key in settings)
               if (key, path) in EFFECTS and _path(have_box.get(key)) != path]
    if effects:
        answer["effect"] = " ".join(effects)
    return answer


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/self/probes"): ("admin", lambda request: report()),
}
