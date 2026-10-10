"""How the hosts share the work: one choice for the whole cluster.

Placement is otherwise set app by app, and with thirty apps on two or three
hosts nobody assembles a plan for the cluster that way (#347). This is the
cluster's answer, settings.placement.mode:

* default - Kubernetes as it is: nothing here changes anything.
* spread - hosts carry apps in proportion to their size. The scheduler
  already does this (it scores hosts by what is left after reservations), but
  only for apps that reserve something, so this mode says how many do not and
  offers the reservations review. No app is changed by choosing it.
* standby - the hosts named in standby_nodes take apps only when the others
  cannot: each carries the taint homestead.io/standby=true:PreferNoSchedule,
  which the scheduler avoids while it has a choice and ignores when it has
  none. Nothing in any app changes, so it works the same for apps Flux, Argo
  CD or Helm apply. When a host comes back, the rebalance moves apps off the
  standby hosts first (homestead_recovery).

PreferNoSchedule rather than NoSchedule: a hard taint would also keep
DaemonSets - Longhorn's, Homestead's probe - from their pods on that host
the next time they roll out. A host is never made standby when that would
leave no other host taking work.

Homestead keeps the taints matching the setting: it adds its own where the
setting says, and removes its own - never anyone else's - where it does not.
"""
import re

MODES = ("", "default", "spread", "standby")
TAINT = {"key": "homestead.io/standby", "value": "true", "effect": "PreferNoSchedule"}
MAX_STANDBY = 10
NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")

kget = ksend = None


def bind(_kget, _ksend):
    global kget, ksend
    kget, ksend = _kget, _ksend


def validate(value):
    """settings.placement's mode and standby hosts, checked; "" is a mode not
    chosen yet, which the setup guide offers and behaves as default."""
    mode = str((value or {}).get("mode", "") or "")
    if mode not in MODES:
        raise ValueError("how hosts share the work must be default, spread or standby")
    standby = (value or {}).get("standby_nodes") or []
    if not isinstance(standby, list) or len(standby) > MAX_STANDBY \
            or any(not isinstance(n, str) or not NAME.match(n) for n in standby):
        raise ValueError("standby hosts must be a list of host names")
    if mode == "standby" and not standby:
        raise ValueError("choose at least one standby host, or another way to share the work")
    return {"mode": mode, "standby_nodes": sorted(set(standby))}


def _ours(taint):
    return taint.get("key") == TAINT["key"]


def _takes_work(node):
    ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                for c in (node.get("status") or {}).get("conditions") or [])
    return ready and not (node.get("spec") or {}).get("unschedulable")


def standby_of(placement):
    """The hosts that are standby under the setting: none unless in standby mode."""
    placement = placement or {}
    return set(placement.get("standby_nodes") or []) if placement.get("mode") == "standby" else set()


def wanted(nodes, placement):
    """The hosts that should carry the taint, and why any named one does not.
    A standby host is never the last one taking work."""
    named = standby_of(placement)
    names = {(n.get("metadata") or {}).get("name") for n in nodes}
    primaries = [n for n in nodes if (n.get("metadata") or {}).get("name") not in named and _takes_work(n)]
    notes = [f"{name} is not a host in this cluster" for name in sorted(named - names)]
    if named and not primaries:
        return set(), notes + ["no other host takes work, so no host is made standby"]
    return named & names, notes


def reconcile(nodes, placement):
    """Adds Homestead's standby taint where the setting says and removes it
    where it does not. Returns what changed, and notes."""
    want, notes = wanted(nodes, placement)
    changed = []
    for node in nodes:
        meta, spec = node.get("metadata") or {}, node.get("spec") or {}
        name = meta.get("name")
        taints = spec.get("taints") or []
        has = any(_ours(t) for t in taints)
        if (name in want) == has:
            continue
        kept = [t for t in taints if not _ours(t)]
        new = kept + [dict(TAINT)] if name in want else kept
        # A merge patch replaces the list; it is the node's own list with only
        # Homestead's taint added or taken out.
        ksend("PATCH", f"/api/v1/nodes/{name}", {"spec": {"taints": new or None}},
              ctype="application/merge-patch+json")
        changed.append(f"{name} {'is now standby' if name in want else 'takes apps again'}")
    return changed, notes


def summary(nodes, placement, unreserved=None):
    """For Settings and the setup guide: the mode, each host's part, and how
    many apps reserve nothing (which spread depends on)."""
    want, notes = wanted(nodes, placement)
    hosts = []
    for node in nodes:
        name = (node.get("metadata") or {}).get("name")
        tainted = any(_ours(t) for t in (node.get("spec") or {}).get("taints") or [])
        hosts.append({"name": name, "standby": name in want, "tainted": tainted, "takes_work": _takes_work(node)})
    return {"mode": (placement or {}).get("mode", ""), "hosts": sorted(hosts, key=lambda h: h["name"]),
            "notes": notes, "unreserved": unreserved,
            "applies": sum(1 for h in hosts if h["takes_work"]) >= 2 or bool(want)}
