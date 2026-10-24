"""Outage webhooks saved before Allow a LAN address existed keep working (#741).

Before #741 a webhook reached a LAN address without anyone allowing it; now a
call refuses one unless the webhook carries "lan". So, once, the leader gives
the LAN allowance to each webhook still in the old shape (no "format": every
save since records one) whose address resolves to a LAN address now. Only the
LAN allowance: an address a webhook never reaches stays refused, and a public
one is left as it is.

Once means once per cluster. What it has done is kept in a ConfigMap of
Homestead's own (homestead-outage-carry, in Homestead's namespace), written
with homestead_kube.update_json like Homestead's other ConfigMaps, so neither
a restart nor another copy taking the lead runs it again. Nothing on /data.

- It acts only on lists it really read: the apps and the VMs read here with
  homestead_kube.items, which raises on a failed or cut-short list (a cluster
  without KubeVirt has no VMs). The page's cached lists cannot tell a VM list
  that failed from no VMs.
- A name that does not resolve, or a write that fails, is tried again each
  round (EVERY) for a day from the first try, which is kept with the rest.
  After that it stops, and each webhook left is an alert until an admin
  ticks Allow a LAN address or the webhook is saved again.
- An address that resolves to the LAN and to one never reached is left: the
  allowance would not make it reachable. It is an alert too.
- Nothing is looked up unless the ConfigMap could be read and written first:
  while it cannot, it is tried less and less often (up to MAX_WAIT).
- Each write names the object's resourceVersion: a save made meanwhile wins.
- At most MAX_LOOKUPS names are looked up a round (getaddrinfo has no
  timeout), those not looked up for longest first.
- Whatever is stored, it never raises: a webhook that is not text is left
  alone, and a kept state that is not what it wrote (not JSON, a first try
  that is not a time) starts again. outage_tick calls it after the due
  actions, and guarded, so it can never hold them up or stop them.
"""
import json
import threading
import time

import homestead_kube as KUBE
import homestead_names as NAMES
import homestead_outage as OUTAGE
import homestead_routes as ROUTER

STATE_KEY = "lan-carry.json"
WINDOW = 24 * 3600
EVERY = 300
MAX_WAIT = 3600
MAX_LOOKUPS = 20
DEPLOYMENTS = "/apis/apps/v1/deployments"
VMS = "/apis/kubevirt.io/v1/virtualmachines"
PREFIX = "outage-lan:"
# This copy's view: done (True/False once read), left {key: why}, next (when
# to try again), wait (the back-off), unsaved (a finished state not yet kept),
# looked {key: round} and given_up {key: why} for the lookups bound.
_carry = {}
_lock = threading.Lock()


def _store():
    return f"/api/v1/namespaces/{OUTAGE._bound['ns']}/configmaps/{NAMES.object_name('outage-carry')}"


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _left(value):
    """{key: why} as kept, without anything that is not one."""
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str) and _named(k)}


def _named(key):
    kind, _, ref = key.partition(":")
    ns, _, name = ref.partition("/")
    return kind in ("app", "vm") and bool(ns) and bool(name)


def _read():
    """What was done, as kept in the cluster; {} before the first try, and
    {"broken": True} when what is kept is not what this wrote. Raises when it
    cannot be read."""
    found = KUBE.get_optional(_store(), read=OUTAGE._bound["kget"])
    raw = ((found or {}).get("data") or {}).get(STATE_KEY)
    try:
        value = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {"broken": True}
    return value if isinstance(value, dict) else {"broken": True}


def _write(change, replace=False):
    return KUBE.update_json(_store(), STATE_KEY, lambda current: change(current if isinstance(current, dict) else {}),
                            replace=replace, read=OUTAGE._bound["kget"], write=OUTAGE._bound["ksend"])


def _backoff(wait):
    return min(max(2 * wait, 2 * EVERY), MAX_WAIT)


def _old(actions):
    """A webhook saved before #741: no format recorded and no LAN allowance."""
    return bool(actions.get("webhook")) and "format" not in actions and "lan" not in actions


def _carryable(actions):
    """An old one whose address is text: anything else no call could use either."""
    return _old(actions) and isinstance(actions["webhook"], str)


def _reach(url):
    """What kinds of address url resolves to ({"lan", "public", "never"}); None when it does not resolve."""
    try:
        return {OUTAGE._kind(ip) for ip in OUTAGE._addresses(url)[1]}
    except (OSError, ValueError):
        return None


def _result(done, marked=(), pending=(), left=None):
    return {"done": done, "marked": list(marked), "pending": list(pending), "left": dict(left or {})}


def _say_once(what, text):
    if _carry.get("said") != what:
        _carry["said"] = what
        print(f"outage actions: {text}", flush=True)


def carry_lan(now=None):
    """One round, on the leader (outage_tick). None when it is not time to try."""
    now = time.time() if now is None else now
    try:
        return _round(now)
    except Exception as error:      # never reached; a bug here must not loop or spread
        _carry["next"] = now + EVERY
        _say_once("bug", f"carry-over of webhooks saved before #741 failed this round: {type(error).__name__}")
        return _result(False)


def _round(now):
    if _carry.get("done") and not _carry.get("unsaved"):
        return _result(True, left=_carry.get("left"))
    if now < _carry.get("next", 0):
        return None
    if _carry.get("unsaved"):
        return _keep(_carry["unsaved"], now)
    try:
        state = _read()
        if _number(state.get("done")) and not state.get("broken"):
            _carry.update(done=True, left=_left(state.get("left")))
            return _result(True, left=_carry["left"])
        if not _number(state.get("first")) or state.get("broken"):
            # Before the first try, or kept in a shape this never wrote: the day starts now.
            state = _write(lambda current: {"first": current["first"] if _number(current.get("first")) else int(now)},
                           replace=bool(state.get("broken")))
    except Exception as error:
        # Where it is kept cannot be read or written: no lookups, and less often each time.
        _carry["wait"] = _backoff(_carry.get("wait", 0))
        _carry["next"] = now + _carry["wait"]
        _say_once("store", f"webhooks saved before #741 not checked yet: {str(error)[:120]}")
        return _result(False)
    _carry.update(done=False, wait=0, next=now + EVERY)
    kget = OUTAGE._bound["kget"]
    try:
        found = [("app", d) for d in KUBE.items(DEPLOYMENTS, read=kget)] + \
                [("vm", v) for v in KUBE.items(VMS, "kubevirt.io", read=kget)]
    except Exception as error:
        _say_once("lists", f"webhooks saved before #741 not checked yet: {str(error)[:120]}")
        return _result(False)
    expired = now - state["first"] >= WINDOW
    marked, pending, left, odd = [], [], {}, []
    given_up = _carry.setdefault("given_up", {})
    looked = _carry.setdefault("looked", {})
    _carry["round"] = turn = _carry.get("round", 0) + 1
    old = []
    for kind, obj in found:
        meta = obj.get("metadata") or {}
        actions = OUTAGE.stored(meta.get("annotations") or {}, NAMES) or {}
        key = f"{kind}:{meta.get('namespace')}/{meta.get('name')}"
        if _old(actions) and not _carryable(actions):
            odd.append(key)
        elif _carryable(actions):
            old.append((key, kind, meta, actions))
    if odd:
        _say_once(("odd", tuple(odd)), "an outage webhook that is not an address, left as it is: " + ", ".join(odd))
    # Those given up on are not looked up again; of the rest, those not looked up for longest first.
    old.sort(key=lambda row: looked.get(row[0], 0))
    budget = MAX_LOOKUPS
    for key, kind, meta, actions in old:
        if key in given_up:
            left[key] = given_up[key]
            continue
        if not budget:
            pending.append(key)
            continue
        budget -= 1
        looked[key] = turn
        kinds = _reach(actions["webhook"])
        if kinds is not None and "lan" not in kinds:
            continue            # public, or only never reached: the allowance changes nothing
        if kinds and "never" in kinds:
            left[key] = given_up[key] = "never"
            continue
        why = "unresolved"
        if kinds:
            patch = {"annotations": {NAMES.key(OUTAGE.KEY): json.dumps({**actions, "lan": True}, separators=(",", ":"))}}
            if meta.get("resourceVersion"):
                patch["resourceVersion"] = meta["resourceVersion"]
            try:
                OUTAGE._bound["ksend"]("PATCH", OUTAGE._path_of(kind, meta.get("namespace"), meta.get("name")),
                                       {"metadata": patch}, ctype="application/merge-patch+json")
                marked.append(key)
                continue
            except Exception as error:
                why = "not saved"
                print(f"outage actions: {key}'s webhook not carried over yet ({str(error)[:120]})", flush=True)
        if expired:
            left[key] = given_up[key] = why
        else:
            pending.append(key)
    if marked:
        ROUTER.forget("wl", "vms")
        print(f"outage actions: {len(marked)} webhook(s) saved before #741 keep reaching their LAN address", flush=True)
    if pending:
        _say_once(("pending", tuple(pending)), "not carried over yet, tried again until a day after the first try: "
                  + ", ".join(pending))
        return _result(False, marked, pending)
    if left:
        print("outage actions: webhooks saved before #741 left as they were (an alert says so): " + ", ".join(sorted(left)), flush=True)
    return _keep({"first": state["first"], "done": int(now), "left": left}, now, marked)


def _keep(final, now, marked=()):
    """Done: this copy stops here whatever happens; the cluster is told until it has kept it."""
    with _lock:
        _carry.update(done=True, left=dict(final["left"]))
    try:
        _write(lambda current: {**final, "left": {k: v for k, v in final["left"].items() if k in _carry["left"]}})
        _carry.pop("unsaved", None)
        _carry["wait"] = 0
    except Exception as error:
        _carry["unsaved"] = final
        _carry["wait"] = _backoff(_carry.get("wait", 0))
        _carry["next"] = now + _carry["wait"]
        _say_once("unsaved", f"carry-over of webhooks saved before #741 done, but not kept yet: {str(error)[:120]}")
    return _result(True, marked, left=final["left"])


def _fact(key, why):
    kind, ref = key.split(":", 1)
    ns, name = ref.split("/", 1)
    what = f"VM {name}" if kind == "vm" else name
    href = f"/vms?panel=monitoring&ns={ns}&vm={name}" if kind == "vm" else f"/containers?panel=monitoring&ns={ns}&workload={name}"
    if why == "never":
        title = f"The outage webhook for {what} needs a new address"
        body = ("It was saved before Homestead asked for Allow a LAN address. Its address also resolves to one a "
                "webhook never reaches (this host, link-local or a cloud metadata address), so Homestead no longer "
                "calls it, and allowing a LAN address would not change that. Give it an address on the LAN only.")
    else:
        title = f"The outage webhook for {what} needs Allow a LAN address"
        body = ("It was saved before Homestead asked for Allow a LAN address, and in the day after the upgrade "
                + ("its address could not be resolved" if why == "unresolved" else "the allowance could not be saved")
                + ", so Homestead no longer calls it if the address is on the LAN. An admin can tick Allow a LAN "
                  "address in its Monitoring dialog, or save the webhook again.")
    return {"key": PREFIX + key, "category": "degraded", "severity": "degraded", "title": title, "body": body,
            "resolved": f"The outage webhook for {what} is set", "href": href, "signals": {"condition": 1}}


def alert_facts():
    """An alert for each webhook the carry-over left, until it is allowed the LAN or saved again."""
    if "done" not in _carry:
        try:
            state = _read()
        except Exception:
            return [{"unknown_prefix": PREFIX}]
        if not _number(state.get("done")) or state.get("broken"):
            return []
        with _lock:
            _carry.setdefault("done", True)
            _carry.setdefault("left", _left(state.get("left")))
    if not _carry.get("done"):
        return []
    facts, cleared = [], []
    for key, why in sorted(_left(_carry.get("left")).items()):
        kind, ref = key.split(":", 1)
        ns, name = ref.split("/", 1)
        try:
            obj = KUBE.get_optional(OUTAGE._path_of(kind, ns, name), read=OUTAGE._bound["kget"])
        except Exception:
            facts.append({"unknown_key": PREFIX + key})
            continue
        actions = OUTAGE.stored(((obj or {}).get("metadata") or {}).get("annotations") or {}, NAMES) or {}
        if obj and _old(actions):
            facts.append(_fact(key, why))
        else:
            cleared.append(key)
    if cleared:
        with _lock:
            for key in cleared:
                _carry["left"].pop(key, None)
        if not _carry.get("unsaved"):
            try:
                _write(lambda current: {**current, "left": {k: v for k, v in (current.get("left") or {}).items()
                                                            if k not in cleared}})
            except Exception:
                pass        # this copy has forgotten them; another finds them cleared again
    return facts
