"""Priority tiers for a group of apps, and where each app's tier comes from (#430).

A group is an annotation on each Deployment (homestead.io/group), so it needs
no list of its own: a group lasts while something is in it. A group's tier is
an entry in one ConfigMap of Homestead's, homestead-group-tiers, key
group-tiers.json: {"<group>": "critical" | "normal" | "low"}. A group with no
tier has no entry. Like the portal's links, it is kept apart from the
settings, since it is a map that grows. It is not in git: a cluster rebuilt
from git keeps each app's class, but its group tiers must be set again.

An app follows its group when it carries homestead.io/tier: group. Its tier is
then its group's, or the cluster default (normal, no class) when the group has
none; homestead.io/tier-class records the class Homestead wrote for it, so a
class someone changed afterwards is told apart from a group that changed. An
app without the marker has a tier of its own, read from its class. So the
order is the app's own tier, its group's, then the default, and a class alone
never means "from the group". Apps Flux or Argo CD apply never get the
marker, but their tier is compared with their group's. VMs have no groups.

Setting a group's tier writes only the map: nothing restarts. Priority tiers
then lists the apps that need a restart, and its reviewed job makes them.
"""
import json
import urllib.error

import homestead_names as NAMES
# homestead_priority imports this module too; each reads the other's names
# only when called, so either may be imported first.
import homestead_priority as PRIORITY

DATA_KEY = "group-tiers.json"
TIERS = ("critical", "normal", "low")
GROUP = NAMES.key("group")        # homestead.io/group: the group an app is in
FOLLOW = NAMES.key("tier")        # homestead.io/tier: group - it follows its group
WROTE = NAMES.key("tier-class")   # homestead.io/tier-class: the class Homestead wrote for it
DEPLOYMENTS = "/apis/apps/v1/deployments"

# server.py's helpers, bound by name (bind), so a test that replaces one in
# server.py replaces it here too; and its values.
NEEDS = ("kget", "ksend", "guard_managed_smb", "_dns_name", "is_self")
kget = ksend = guard_managed_smb = _dns_name = None
is_self = lambda ns, name: False
managed_by = lambda dep: {}
NAMESPACE = "default"
OWN_GROUP = "Homestead"


def bind(helpers, *, namespace, own_group, managed):
    global NAMESPACE, OWN_GROUP, managed_by
    missing = set(NEEDS) - set(helpers)
    if missing:
        raise TypeError("homestead_group_tiers needs " + ", ".join(sorted(missing)))
    globals().update({name: helpers[name] for name in NEEDS})
    NAMESPACE, OWN_GROUP, managed_by = namespace, own_group, managed


def _map():
    return NAMES.object_name("group-tiers")


def _path():
    return f"/api/v1/namespaces/{NAMESPACE}/configmaps/{_map()}"


def _meta(obj):
    return (obj or {}).get("metadata") or {}


def _annotations(dep):
    return _meta(dep).get("annotations") or {}


def _spec(dep):
    return (((dep or {}).get("spec") or {}).get("template") or {}).get("spec") or {}


def group_of(dep):
    """The group an app is in, or ""."""
    return _annotations(dep).get(GROUP) or ""


def follows(dep):
    """Whether an app carries the marker: it follows its group."""
    return _annotations(dep).get(FOLLOW) == "group"


def read(get=None):
    """(tiers, resourceVersion). No map yet is ({}, None). Raises when the map
    can't be read; tiers is None when what it holds isn't a map of tiers."""
    try:
        cm = (get or kget)(_path())
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return {}, None
        raise
    version = _meta(cm).get("resourceVersion")
    try:
        raw = json.loads(((cm or {}).get("data") or {}).get(DATA_KEY) or "{}")
    except ValueError:
        return None, version
    if not isinstance(raw, dict):
        return None, version
    return {str(group): tier for group, tier in raw.items() if tier in TIERS}, version


def stored(get=None):
    """The group tiers, or None when they can't be read just now - never an
    empty map in its place. get: the reader to use; the plan and Edit pass
    their own module's."""
    try:
        return read(get)[0]
    except Exception:
        return None


def _class(tier, lad):
    try:
        return PRIORITY.class_for(tier, lad)
    except ValueError:
        return None   # no class can rank there; Priority tiers' warnings say so


def resolve(dep, tiers, ladder, *, kind="app", managed=False):
    """Where an app's tier comes from: {tier, class, source, runs}.

    tier and class: what it should run with (class None for no class).
    source: "own" without the marker; "group" with it, when its group has a
    tier; "default" with it, when it has no group or the group has no tier.
    runs: the tier its template has now. tiers is None when the map can't be
    read: an app that follows a group then has tier None. managed: Flux or
    Argo CD applies it - it never follows, but its group's tier, if there is
    one, is what it should run with. VMs are always their own."""
    runs, cls = PRIORITY.tier_of(_spec(dep), ladder)
    own = {"tier": runs, "class": cls or None, "source": "own", "runs": runs}
    if kind == "vm":
        return own
    group = group_of(dep)
    following = follows(dep) and not managed
    if not following and not (managed and group):
        return own
    if group and tiers is None:
        return {"tier": None, "class": None, "source": "group", "runs": runs}
    tier = tiers.get(group) if group else None
    if tier:
        return {"tier": tier, "class": _class(tier, ladder), "source": "group", "runs": runs}
    if managed:
        return own
    return {"tier": "normal", "class": None, "source": "default", "runs": runs}
