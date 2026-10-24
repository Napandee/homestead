"""Apps that git applies: Flux or Argo CD, or an administrator's mark that
something else does (homestead_updates.managed_by).

A change Homestead makes to such an app lasts until that tool next applies
it, usually within minutes. A review that writes an app's Deployment says so
first (#734), and a rename, which that tool would turn into two copies,
needs its own acknowledgement.
"""
import copy
import json
import re

import homestead_names as NAMES
import homestead_updates as UPDATES

# Every review that writes a Deployment's spec. Each answers with "managed",
# so the page and the MCP tools both see who applies the app
# (tests/test_gitops_notice.py keeps this list complete).
REVIEWS = (("POST", "/api/edit/preview"), ("POST", "/api/move/preview"))


def notice(dep):
    """Who applies this app instead of Homestead: {} when nobody does."""
    return UPDATES.managed_by(dep or {})


def require_rename_ack(dep, body):
    """A rename makes the app under its new name and removes the old one;
    Flux or Argo CD then brings the old one back. Refused until the person
    (or the tool acting for them) says they know."""
    managed = notice(dep)
    if managed and body.get("confirm_managed") is not True:
        where = f" ({managed['source']})" if managed.get("source") else ""
        raise ValueError(f"{managed['by']} applies this app{where} and will bring back the old name, so there "
                         "would be two copies. Rename it in git instead, or confirm with confirm_managed: true")


# ------------------------------------------------- the change, as YAML (#735)
# What Homestead keeps for itself on an app - update history, the digests it
# last ran, restart stamps - is not a setting a person commits; the settings
# they chose (group, logo, monitoring) are, and stay in.
BOOKKEEPING = frozenset((UPDATES.TRACKED, UPDATES.PREVIOUS, UPDATES.LAST_ACTION, UPDATES.RAN, UPDATES.ROLLOUT_AT,
                         NAMES.key("editedAt"), "kubectl.kubernetes.io/restartedAt", "deployment.kubernetes.io/revision"))
_UNCHANGED = object()
# Anything a YAML 1.1 reader (Kubernetes, Flux) or a 1.2 one could take for
# something other than a string: numbers in any base, sexagesimal, bools and
# their yes/no/on/off cousins, nulls, infinities.
_TYPED = re.compile(r"[-+]?(\.?[0-9][0-9_.:eE+-]*|0[xXoObB][0-9a-fA-F_]+|\.(inf|Inf|INF|nan|NaN|NAN))"
                    r"|y|Y|yes|Yes|YES|n|N|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF"
                    r"|null|Null|NULL|~|<<|=")


def _scalar(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    text = str(value)
    plain = (text and text == text.strip() and not _TYPED.fullmatch(text) and text[0] not in "-?:,[]{}#&*!|>'\"%@`"
             and ": " not in text and " #" not in text and not text.endswith(":")
             and all(ch.isprintable() for ch in text))
    return text if plain else json.dumps(text)


def _lines(value, indent):
    pad = " " * indent
    if isinstance(value, dict):
        out = []
        for key, item in value.items():
            head = f"{pad}{_scalar(key)}:"
            if isinstance(item, (dict, list)) and item:
                out.append(head)
                out += _lines(item, indent + 2 if isinstance(item, dict) else indent)
            else:
                out.append(f"{head} {'{}' if item == {} else '[]' if item == [] else _scalar(item)}")
        return out
    out = []
    for item in value:
        if isinstance(item, (dict, list)) and item:
            inner = _lines(item, indent + 2)
            out.append(f"{pad}- {inner[0].lstrip()}")
            out += inner[1:]
        else:
            out.append(f"{pad}- {'{}' if item == {} else '[]' if item == [] else _scalar(item)}")
    return out


def dump(value):
    """Block YAML for plain data, with no library: Homestead runs on the
    standard library alone. A string is quoted whenever a reader could type
    it as anything else."""
    return "\n".join(_lines(value, 0))


def _named(rows):
    return isinstance(rows, list) and rows and all(isinstance(r, dict) and isinstance(r.get("name"), str) for r in rows)


def _diff(old, new):
    if old == new:
        return _UNCHANGED
    if isinstance(old, dict) and isinstance(new, dict):
        out = {}
        for key, value in new.items():
            change = _diff(old.get(key, _UNCHANGED), value) if key in old else value
            if change is not _UNCHANGED:
                out[key] = change
        out.update({key: None for key in old if key not in new})
        return out or _UNCHANGED
    if _named(old) and _named(new):
        # Containers, env, volumes: merged by name, as a strategic merge patch
        # (and a kustomize patch) does. One gone is deleted by name.
        before = {row["name"]: row for row in old}
        out = []
        for row in new:
            change = _diff(before[row["name"]], row) if row["name"] in before else row
            if change is not _UNCHANGED:
                out.append({"name": row["name"], **change})
        names = {row["name"] for row in new}
        out += [{"name": row["name"], "$patch": "delete"} for row in old if row["name"] not in names]
        return out or _UNCHANGED
    return copy.deepcopy(new)


def _settings(meta):
    return {key: dict((value for value in (meta.get(key) or {}).items() if value[0] not in BOOKKEEPING))
            for key in ("labels", "annotations")}


def patch(current, proposed):
    """Only what changed from current to proposed: spec, and the labels and
    annotations that are settings. {} when nothing a person commits did."""
    out = {}
    meta = _diff(_settings(current.get("metadata") or {}), _settings(proposed.get("metadata") or {}))
    if meta is not _UNCHANGED:
        out["metadata"] = meta
    old_spec, new_spec = copy.deepcopy(current.get("spec") or {}), copy.deepcopy(proposed.get("spec") or {})
    for spec in (old_spec, new_spec):
        template = (spec.get("template") or {}).get("metadata")
        if isinstance(template, dict):
            template.update(_settings(template))
    spec = _diff(old_spec, new_spec)
    if spec is not _UNCHANGED:
        out["spec"] = spec
    return out


def document(dep, body):
    """body under the object's kind, name and namespace: a patch to commit
    where Flux or Argo CD reads the app from - a kustomize patch, or the same
    fields in the Deployment itself."""
    meta = dep.get("metadata") or {}
    head = {"apiVersion": dep.get("apiVersion") or "apps/v1", "kind": dep.get("kind") or "Deployment",
            "metadata": {"name": meta.get("name", ""), "namespace": meta.get("namespace", ""),
                         **((body.get("metadata") or {}))}}
    return dump({**head, **{key: value for key, value in body.items() if key != "metadata"}})


def git_image(item):
    """An update check's image line for git: the new tag, or - when only what
    the tag points at moved - the tag pinned to the new digest."""
    if item.get("candidate") != item.get("deployed") or not item.get("remote_digest"):
        return item["candidate"]
    return f"{item['candidate'].split('@', 1)[0]}@{item['remote_digest']}"


def image_yaml(dep, images):
    """An image bump to commit: the image: line of each container that
    changes ({container: image}), init containers under their own key."""
    pod = ((dep.get("spec") or {}).get("template") or {}).get("spec") or {}
    spec = {}
    for key in ("initContainers", "containers"):
        rows = [{"name": c["name"], "image": images[c["name"]]} for c in pod.get(key) or [] if c.get("name") in images]
        if rows:
            spec[key] = rows
    return document(dep, {"spec": {"template": {"spec": spec}}})
