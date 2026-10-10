"""Moving containers back after a host returns.

When a host fails or is drained, its containers start on the others, and
nothing moves them back when it returns: the cluster stays lopsided until
someone opens Balance hosts (#347). This watches each host and notices one
coming back - Ready again after not being, or taking work again after being
cordoned - and then, as settings.placement.rebalance says:

* off: nothing.
* suggest: an alert says the host is back and how many containers the
  container rebalance would move; nothing moves until someone reviews it.
* window: the same alert, and in the next maintenance window the rebalance
  runs by itself, as the job a person would start, leaving out any app whose
  backup job is due before the window closes - a backup has to run on the
  app's host while its volume is attached there, and a move under it would
  fail it or the move.

A returned host is remembered for RETURN_KEEP seconds; once the rebalance
finds nothing worth moving, or has run, it is forgotten.
"""
import calendar
import datetime
import json
import os
import threading
import time
import zoneinfo

import homestead_shared as SHARED

DATA_DIR = "/data"
FILE = "recovery.json"
MODES = ("off", "suggest", "window")
RETURN_KEEP = 24 * 3600
_lock = threading.Lock()


def bind(data_dir):
    global DATA_DIR
    DATA_DIR = data_dir


def _path():
    return os.path.join(DATA_DIR, FILE)


def _load():
    try:
        with open(_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return {"hosts": data.get("hosts") or {}, "returned": data.get("returned") or {},
                    "suggestion": data.get("suggestion") or {}, "ran": data.get("ran") or ""}
    except (OSError, ValueError):
        pass
    return {"hosts": {}, "returned": {}, "suggestion": {}, "ran": ""}


def _save(data):
    SHARED.write_json(_path(), data, separators=(",", ":"), sort_keys=True)


# ------------------------------------------------------------- the window
def window(maintenance, now):
    """Whether the maintenance window is open at now, and when this opening
    closes (0 when shut). A window may cross midnight: its early minutes then
    belong to the day it started on."""
    current = time.gmtime(now)
    hour, minute = (int(part) for part in maintenance["start"].split(":"))
    start, duration = hour * 60 + minute, int(maintenance["duration_minutes"])
    current_minute = current.tm_hour * 60 + current.tm_min
    midnight = calendar.timegm((current.tm_year, current.tm_mon, current.tm_mday, 0, 0, 0))
    if current.tm_wday in maintenance["days"] and start <= current_minute < min(1440, start + duration):
        return True, midnight + (start + duration) * 60
    carry = max(0, start + duration - 1440)
    if (current.tm_wday - 1) % 7 in maintenance["days"] and current_minute < carry:
        return True, midnight + carry * 60
    return False, 0


def window_key(maintenance, now):
    """Which opening of the window now falls in, so it runs once per opening."""
    open_, closes = window(maintenance, now)
    return time.strftime("%Y-%m-%dT%H:%M", time.gmtime(closes)) if open_ else ""


# ------------------------------------------------------------- cron
MACROS = {"@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 0 1 * *", "@weekly": "0 0 * * 0",
          "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@hourly": "0 * * * *"}


def _field(text, lo, hi):
    out = set()
    for part in text.split(","):
        rng, _, step = part.partition("/")
        step = int(step) if step else 1
        if rng in ("*", "?"):
            a, b = lo, hi
        elif "-" in rng:
            a, b = (int(x) for x in rng.split("-", 1))
        else:
            a = int(rng)
            b = hi if step > 1 else a
        if step < 1 or a < lo or b > hi or a > b:
            raise ValueError(text)
        out.update(range(a, b + 1, step))
    return out


def fires_between(schedule, start, end, tz=""):
    """Whether a CronJob's schedule fires in [start, end). None when the
    schedule or its time zone cannot be read - the caller treats that as
    'it may', since a backup missed is worse than a move put off."""
    text = MACROS.get(str(schedule).strip(), str(schedule).strip())
    parts = text.split()
    if len(parts) != 5:
        return None
    try:
        minute, hour, dom, month = (_field(parts[0], 0, 59), _field(parts[1], 0, 23),
                                    _field(parts[2], 1, 31), _field(parts[3], 1, 12))
        dow = {d % 7 for d in _field(parts[4], 0, 7)}
    except ValueError:
        return None
    zone = None
    if tz and tz not in ("UTC", "Etc/UTC"):
        try:
            zone = zoneinfo.ZoneInfo(tz)
        except Exception:
            return None
    any_dom, any_dow = parts[2] in ("*", "?"), parts[4] in ("*", "?")
    t = int(start) - int(start) % 60
    while t < end:
        if zone is not None:
            local = datetime.datetime.fromtimestamp(t, zone)
            fields = (local.minute, local.hour, local.day, local.month, (local.weekday() + 1) % 7)
        else:
            g = time.gmtime(t)
            fields = (g.tm_min, g.tm_hour, g.tm_mday, g.tm_mon, (g.tm_wday + 1) % 7)
        m, h, d, mo, wd = fields
        # Cron's rule: with both day fields restricted, either one will do.
        day = (wd in dow) if any_dom else (d in dom) if any_dow else (d in dom or wd in dow)
        if m in minute and h in hour and mo in month and day:
            return True
        t += 60
    return False


def guarded(cronjobs, deployments, start, end):
    """Apps (ns/name) a scheduled job shares a volume with and that job runs,
    or may run, before end - or runs now."""
    claims = {}
    for dep in deployments:
        meta = dep.get("metadata") or {}
        for volume in (((dep.get("spec") or {}).get("template") or {}).get("spec") or {}).get("volumes") or []:
            claim = (volume.get("persistentVolumeClaim") or {}).get("claimName")
            if claim:
                claims.setdefault((meta.get("namespace"), claim), set()).add(f"{meta.get('namespace')}/{meta.get('name')}")
    out = {}
    for cron in cronjobs:
        meta, spec, status = cron.get("metadata") or {}, cron.get("spec") or {}, cron.get("status") or {}
        if spec.get("suspend"):
            continue
        pod = (((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("spec") or {}
        apps = set()
        for volume in pod.get("volumes") or []:
            claim = (volume.get("persistentVolumeClaim") or {}).get("claimName")
            apps |= claims.get((meta.get("namespace"), claim), set())
        if not apps:
            continue
        due = bool(status.get("active")) or fires_between(spec.get("schedule", ""), start, end, spec.get("timeZone") or "")
        if due is not False:
            for app in apps:
                out.setdefault(app, f"its scheduled job {meta.get('name')} runs before the window closes")
    return out


# ------------------------------------------------------------- hosts
def _takes_work(node):
    ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                for c in (node.get("status") or {}).get("conditions") or [])
    return ready and not (node.get("spec") or {}).get("unschedulable")


def observe(nodes, now=None):
    """Notes each host that takes work again; returns the hosts back within
    RETURN_KEEP. A host seen for the first time has not come back."""
    now = int(now or time.time())
    with _lock:
        data = _load()
        seen = {}
        for node in nodes:
            name = (node.get("metadata") or {}).get("name")
            if not name:
                continue
            seen[name] = _takes_work(node)
            if seen[name] and data["hosts"].get(name) is False:
                data["returned"][name] = now
            if not seen[name]:
                data["returned"].pop(name, None)
        data["hosts"] = seen
        data["returned"] = {n: t for n, t in data["returned"].items() if now - t < RETURN_KEEP and n in seen}
        if not data["returned"]:
            data["suggestion"] = {}
        _save(data)
        return dict(data["returned"])


def suggest(moves, now=None):
    """What the rebalance would do now for the hosts back; nothing to move
    forgets them, since the scheduler already evened things out."""
    with _lock:
        data = _load()
        if not data["returned"]:
            return
        if not moves:
            data["returned"], data["suggestion"] = {}, {}
        else:
            data["suggestion"] = {"hosts": sorted(data["returned"]), "moves": len(moves), "at": int(now or time.time())}
        _save(data)


def observe_returned():
    """The hosts back and not yet dealt with: a suggestion with nothing to
    move forgets them."""
    with _lock:
        return dict(_load()["returned"])


def ran(key):
    """The window's rebalance started: the hosts back are dealt with."""
    with _lock:
        data = _load()
        data.update(returned={}, suggestion={}, ran=key)
        _save(data)


def already_ran(key):
    with _lock:
        return bool(key) and _load()["ran"] == key


def alert_facts(mode, maintenance=None):
    """A host back, with containers worth moving back to even things out."""
    if mode not in ("suggest", "window"):
        return []
    with _lock:
        found = _load()["suggestion"]
    if not found.get("moves"):
        return []
    hosts, moves = found["hosts"], found["moves"]
    who = " and ".join(hosts) if len(hosts) < 3 else f"{len(hosts)} hosts"
    then = (f"They move in the next maintenance window ({maintenance['start']} UTC) unless you start it sooner."
            if mode == "window" and maintenance else "Nothing moves until you review it.")
    return [{"key": "recovery:rebalance", "category": "joins", "severity": "info",
             "title": f"{who} {'is' if len(hosts) == 1 else 'are'} back: {moves} container{'' if moves == 1 else 's'} could move to even out the hosts",
             "body": f"Containers started elsewhere while {who} {'was' if len(hosts) == 1 else 'were'} away. "
                     f"Balance hosts shows the moves; each restarts its container once. {then}",
             "resolved": "The hosts are even again", "href": "/containers", "signals": {"moves": moves}}]
