"""Log alerts: a saved log search that raises an alert (spike 12, built).

"More than 10 lines matching 'Unable to read frames' a minute from app X."
Built on homestead_logsearch (no Loki: each running pod's log through the
Kubernetes API) and homestead_alerts (debounced facts, raised and resolved
by the leader).

What shapes it:
- **Counts, never lines, never the query.** An alert carries the rule's
  name, its apps and a number. A log line can hold a password or a token,
  and so can a search for one; an alert's title and body go to alerts.json
  (kept in config backups), to every subscribed device's lock screen and to
  the bell. A name that repeats the query is refused for the same reason.
- **Its own thread, leader only.** The worker reads once a minute; the
  shared alert loop only takes its last facts (alert_facts), so a slow read
  or a slow expression never holds up any other alert source.
- **Bounded work.** At most MAX_RULES rules of MAX_APPS apps; MAX_CONTAINERS
  reads a minute of MAX_BYTES each, only what was written since the last
  read, READ_WORKERS at a time within READ_BUDGET seconds. A regular
  expression is matched in a child process killed after REGEX_SECONDS
  (Python cannot stop a regex mid-match in a thread); plain text, the
  default, is matched here.
- **Not read is not zero.** A rule whose app could not be read, was beyond
  the minute's reads, or whose expression ran out of time says so, keeps a
  null for that minute and is neither raised nor cleared.
- **Ids are the server's.** A client id is accepted only when it is
  well-formed and names an existing rule.
"""
import calendar
import json
import math
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
import homestead_logsearch as LOGSEARCH
import homestead_shared as SHARED

EVERY = 60                      # seconds between reads
MAX_RULES = 20
MAX_APPS = 5                    # per rule
MAX_CONTAINERS = 40             # reads a minute, across every rule
MAX_BYTES = 256 * 1024          # per read
READ_WORKERS = 4
READ_BUDGET = 40                # seconds a round's reads may take
REGEX_SECONDS = 2               # per rule and round
MAX_BACK = 300                  # seconds a late round reads back at most
KEEP = 60                       # minutes of counts kept per rule
STALE = 180                     # facts older than this: could not look
WINDOWS = (1, 5, 15)            # minutes a threshold is counted over
SEVERITIES = ("info", "degraded", "critical")
_ID = re.compile(r"^la-[0-9a-f]{12}$")
_COUNTED = ("apps", "query", "regex", "case")   # an edit to these starts the counts again
_lock = SHARED.SharedLock("logalerts")
_round = threading.Lock()
_starting = threading.Lock()
_cursors = {}                   # (ns, pod, container) -> (last stamp read, when); leader memory only
_last = {"at": 0.0, "facts": None}
_started = []
_bound = {"dir": None, "workloads": None, "read": None, "is_leader": None, "beat": None}


class TooSlow(ValueError):
    pass


def bind(data_dir=None, workloads=None, read=None, is_leader=None, beat=None):
    _bound.update(dir=data_dir, workloads=workloads, read=read, is_leader=is_leader, beat=beat)


def reset():
    """Forget the leader's memory: cursors and the last round."""
    _cursors.clear()
    _last.update(at=0.0, facts=None)


def _workloads():
    if _bound["workloads"]:
        return _bound["workloads"]()
    import homestead_routes as ROUTER
    import homestead_workloads as WL
    return ROUTER.cached("wl", 5, WL.get_workloads)


def _read(*args):
    if _bound["read"]:
        return _bound["read"](*args)
    import homestead_kube as KUBE
    return KUBE.logsearch_read(*args)


def _is_leader():
    if _bound["is_leader"]:
        return _bound["is_leader"]()
    import homestead_leader as LEADER
    return LEADER.is_leader()


def _beat(error=None):
    if _bound["beat"]:
        return _bound["beat"]("logalerts", EVERY, error, leader_only=True)
    import homestead_heartbeats as HEART
    return HEART.beat("logalerts", EVERY, error, leader_only=True)


def _path():
    return os.path.join(_bound["dir"] or SHARED.DIR, "logalerts.json")


def _load():
    state = SHARED.read_json(_path(), {})
    state.setdefault("rules", [])
    state.setdefault("counts", {})   # rule id -> [[minute, count or None], ...]
    state.setdefault("status", {})   # rule id -> {"state", "detail", "at"}
    return state


def _save(state):
    SHARED.write_json(_path(), state)


def _apps(workloads):
    return {f"{w.get('ns')}/{w.get('name')}" for w in workloads or []}


def validate(body, known_apps=None, existing_ids=()):
    """A rule from a form, or ValueError."""
    body = body if isinstance(body, dict) else {}
    rule_id = body.get("id")
    if rule_id is None:
        rule_id = "la-" + secrets.token_hex(6)
    elif not (isinstance(rule_id, str) and _ID.match(rule_id) and rule_id in existing_ids):
        raise ValueError("that log alert no longer exists; refresh and try again")
    name = str(body.get("name") or "").strip()
    if not name or len(name) > 60:
        raise ValueError("give the alert a name of at most 60 characters")
    apps = body.get("apps") if isinstance(body.get("apps"), list) else []
    apps = [a for a in apps if isinstance(a, str) and "/" in a]
    if not apps:
        raise ValueError("choose the app (or up to five apps) to watch")
    if len(apps) > MAX_APPS:
        raise ValueError(f"a log alert watches at most {MAX_APPS} apps")
    if known_apps is not None and any(a not in known_apps for a in apps):
        raise ValueError("one of those apps is not running here")
    query, regex, case = str(body.get("query") or ""), bool(body.get("regex")), bool(body.get("case"))
    LOGSEARCH.matcher(query, regex, case)
    if len(query.strip()) >= 3 and query.strip().lower() in name.lower():
        raise ValueError("the name may not repeat the search text: alert names reach lock screens and config backups")
    try:
        threshold, window = int(body.get("threshold")), int(body.get("window") or 1)
    except (TypeError, ValueError):
        raise ValueError("the threshold is a number of lines, from 1") from None
    if not 1 <= threshold <= 100000:
        raise ValueError("the threshold is a number of lines, from 1")
    if window not in WINDOWS:
        raise ValueError("count over 1, 5 or 15 minutes")
    severity = body.get("severity") if body.get("severity") in SEVERITIES else "degraded"
    return {"id": rule_id, "name": name, "apps": sorted(set(apps)), "query": query, "regex": regex, "case": case,
            "threshold": threshold, "window": window, "severity": severity}


def save(body, known_apps=None):
    known = _apps(_workloads()) if known_apps is None else known_apps
    with _lock:
        state = _load()
        old = {r["id"]: r for r in state["rules"]}
        rule = validate(body, known, old)
        if rule["id"] not in old and len(old) >= MAX_RULES:
            raise ValueError(f"at most {MAX_RULES} log alerts")
        before = old.get(rule["id"])
        if before and any(before.get(k) != rule[k] for k in _COUNTED):
            state["counts"].pop(rule["id"], None)
            state["status"].pop(rule["id"], None)
        state["rules"] = [rule if r["id"] == rule["id"] else r for r in state["rules"]] + ([] if before else [rule])
        _save(state)
    return rule


def delete(rule_id):
    with _lock:
        state = _load()
        if not (isinstance(rule_id, str) and _ID.match(rule_id) and any(r["id"] == rule_id for r in state["rules"])):
            raise ValueError("that log alert no longer exists; refresh and try again")
        state["rules"] = [r for r in state["rules"] if r["id"] != rule_id]
        state["counts"].pop(rule_id, None)
        state["status"].pop(rule_id, None)
        _save(state)
    return {"ok": True}


def listing():
    with _lock:
        state = _load()
    return {"rules": [{**r, "counts": state["counts"].get(r["id"], []),
                       "status": state["status"].get(r["id"], {"state": "waiting", "detail": ""})}
                      for r in state["rules"]],
            "every": EVERY, "max_rules": MAX_RULES, "max_apps": MAX_APPS}


# The child that matches an expression: the query and lines on stdin, the
# indexes of the matching lines on stdout. Run with -I: no site, no cwd.
_CHILD = ("import json,re,sys\n"
          "d=json.load(sys.stdin)\n"
          "p=re.compile(d['q'],0 if d['c'] else re.I)\n"
          "sys.stdout.write(json.dumps([i for i,l in enumerate(d['l']) if p.search(l)]))\n")


def matching(lines, query, regex, case, limit=REGEX_SECONDS):
    """The indexes of the lines that match. TooSlow when an expression runs
    out of time: its child process is killed."""
    if not regex:
        needle = query if case else query.lower()
        return [i for i, line in enumerate(lines) if needle in (line if case else line.lower())]
    payload = json.dumps({"q": query, "c": bool(case), "l": list(lines)})
    try:
        done = subprocess.run([sys.executable, "-I", "-c", _CHILD], input=payload, capture_output=True,
                              text=True, timeout=limit, check=False)
    except subprocess.TimeoutExpired:
        raise TooSlow(f"the expression took more than {limit:g} seconds") from None
    if done.returncode != 0:
        raise ValueError("the expression could not be run")
    return json.loads(done.stdout or "[]")


def _since(key, now):
    cursor = _cursors.get(key)
    if not cursor:
        return EVERY
    return max(EVERY, min(MAX_BACK, math.ceil(now - cursor[1]) + 5))


def _read_new(target, now):
    """The lines a container wrote since its cursor, and whether the read was cut."""
    ns, _app, pod, container = target
    key = (ns, pod, container)
    text = _read(ns, pod, container, _since(key, now), MAX_BYTES)
    last = (_cursors.get(key) or ("", 0))[0]
    out, newest = [], last
    for raw in text.splitlines():
        stamp = LOGSEARCH._STAMP.match(raw)
        at, line = (stamp.group(1), stamp.group(2)) if stamp else ("", raw)
        if at and at <= last:
            continue
        newest = max(newest, at)
        out.append(line[:LOGSEARCH.MAX_LINE])
    return key, newest, out, len(text) >= MAX_BYTES


def _plan(rules, workloads):
    """Containers to read this round, rule by rule until MAX_CONTAINERS; the
    apps left out are named."""
    chosen, skipped = [], set()
    for rule in rules:
        for target in LOGSEARCH.targets(workloads, rule["apps"]):
            if target in chosen:
                continue
            if len(chosen) >= MAX_CONTAINERS:
                skipped.add(f"{target[0]}/{target[1]}")
            else:
                chosen.append(target)
    return chosen, skipped


def evaluate(now=None):
    """One round: read what the rules' apps wrote since the last read, count
    each rule's matches, keep the counts and return the alert facts. The
    caller checks leadership."""
    now = time.time() if now is None else now
    with _round:
        with _lock:
            rules = _load()["rules"]
        workloads = _workloads()
        targets, skipped = _plan(rules, workloads)
        lines, unread, capped, cursors = {}, set(), set(), {}
        pool = ThreadPoolExecutor(max_workers=READ_WORKERS)
        futures = {pool.submit(_read_new, t, now): t for t in targets}
        finished, _ = wait(futures, timeout=READ_BUDGET)
        pool.shutdown(wait=False, cancel_futures=True)
        for future, target in futures.items():
            app = f"{target[0]}/{target[1]}"
            try:
                if future not in finished:
                    raise TimeoutError("read took too long")
                key, newest, got, full = future.result()
            except Exception:
                unread.add(app)
                continue
            cursors[key] = (newest, now)
            lines.setdefault(app, []).extend(got)
            if full:
                capped.add(app)
        # A container that is gone, or was not read, starts again from a fresh read.
        _cursors.clear()
        _cursors.update(cursors)
        minute, facts, counts, status = int(now // 60), [], {}, {}
        for rule in rules:
            key = f"logs:{rule['id']}"
            missing = sorted(a for a in rule["apps"] if a in unread)
            later = sorted(a for a in rule["apps"] if a in skipped)
            state, detail, count = "ok", "", None
            if missing:
                state, detail = "unread", "could not read " + ", ".join(a.split("/", 1)[1] for a in missing)
            elif later:
                state, detail = "skipped", (f"not read this minute: over the {MAX_CONTAINERS} containers a minute "
                                            f"Homestead reads for log alerts ({', '.join(a.split('/', 1)[1] for a in later)})")
            else:
                found = [line for app in rule["apps"] for line in lines.get(app, ())]
                try:
                    count = len(matching(found, rule["query"], rule["regex"], rule["case"]))
                except ValueError as error:
                    state, detail = "slow" if isinstance(error, TooSlow) else "failed", str(error)
            counts[rule["id"]] = [minute, count]
            status[rule["id"]] = {"state": state, "detail": detail, "at": int(now)}
            if count is None:
                facts.append({"unknown_key": key})
        with _lock:
            fresh = _load()
            for rule in fresh["rules"]:
                rid = rule["id"]
                if rid not in counts:
                    continue
                history = [c for c in fresh["counts"].get(rid, []) if c[0] > minute - KEEP and c[0] != minute]
                fresh["counts"][rid] = history + [counts[rid]]
                fresh["status"][rid] = status[rid]
                if counts[rid][1] is None:
                    continue
                total = sum(c for m, c in fresh["counts"][rid] if m > minute - rule["window"] and c is not None)
                if total > rule["threshold"]:
                    facts.append(fact(rule, total, capped & set(rule["apps"])))
            ids = {r["id"] for r in fresh["rules"]}
            fresh["counts"] = {k: v for k, v in fresh["counts"].items() if k in ids}
            fresh["status"] = {k: v for k, v in fresh["status"].items() if k in ids}
            _save(fresh)
        facts = [f for f in facts if "unknown_key" not in f or f["unknown_key"][5:] in ids]
        _last.update(at=now, facts=facts)
        return facts


def fact(rule, total, capped=()):
    """What the bell, push and alerts.json see: the rule's name, its apps and
    a count. No line, and no query: a search for a token is the token."""
    apps = ", ".join(a.split("/", 1)[1] for a in rule["apps"])
    span = "a minute" if rule["window"] == 1 else f"{rule['window']} minutes"
    level = max(0, (total // max(1, rule["threshold"])).bit_length() - 1)   # 1x, 2x, 4x... over
    return {"key": f"logs:{rule['id']}", "category": "logs", "severity": rule["severity"],
            "title": f"{rule['name']}: {total} matching lines in {span}",
            "resolved": f"{rule['name']}: back under {rule['threshold']} in {span}",
            "body": f"{apps} wrote more than {rule['threshold']} lines matching this log alert in {span}"
                    + (" (at least: more than Homestead reads at once)" if capped else "")
                    + ". Open Log alerts to search them.",
            "signals": {"level": level}, "href": "/containers?panel=logalerts"}


def alert_facts(now=None):
    """The last round's facts for the shared alert loop, which never reads
    logs itself. None when there is no recent round: could not look."""
    now = time.time() if now is None else now
    if _last["facts"] is None or now - _last["at"] > STALE:
        return None
    return list(_last["facts"])


def step(now=None):
    """One turn of the worker: a round on the leader, nothing elsewhere."""
    if not _is_leader():
        reset()
        return None
    try:
        facts = evaluate(now)
        _beat()
        return facts
    except Exception as error:
        _beat(error)
        print(f"logalerts: {str(error)[:160]}", flush=True)
        return None


def run(sleep=time.sleep):
    while True:
        step()
        sleep(EVERY)


def start():
    """Start the worker once; the alert loop calls this before its own loop."""
    with _starting:
        if _started:
            return
        _started.append(threading.Thread(target=run, name="logalerts", daemon=True))
    _started[0].start()


def preview(body, now=None):
    """What a rule would have done over the last hour: counts per minute and
    the minutes over its threshold. No lines. Takes log search's one-at-a-time
    limit."""
    body = {k: v for k, v in (body if isinstance(body, dict) else {}).items() if k != "id"}
    workloads = _workloads()
    rule = validate(body, _apps(workloads))
    now = time.time() if now is None else now
    with LOGSEARCH.exclusive():
        stamps, found, unread, capped = [], [], set(), set()
        for ns, app, pod, container in LOGSEARCH.targets(workloads, rule["apps"])[:MAX_CONTAINERS]:
            try:
                text = _read(ns, pod, container, 3600, LOGSEARCH.MAX_BYTES)
            except Exception:
                unread.add(app)
                continue
            if len(text) >= LOGSEARCH.MAX_BYTES:
                capped.add(app)
            for raw in text.splitlines():
                stamp = LOGSEARCH._STAMP.match(raw)
                if stamp:
                    stamps.append(stamp.group(1))
                    found.append(stamp.group(2)[:LOGSEARCH.MAX_LINE])
        hits = matching(found, rule["query"], rule["regex"], rule["case"])
    per = {}
    for i in hits:
        at = calendar.timegm(time.strptime(stamps[i][:19], "%Y-%m-%dT%H:%M:%S"))
        per[int(at // 60)] = per.get(int(at // 60), 0) + 1
    minute = int(now // 60)
    counts = [[m, per.get(m, 0)] for m in range(minute - 59, minute + 1)]
    over = [m for m, _ in counts if sum(c for mm, c in counts if m - rule["window"] < mm <= m) > rule["threshold"]]
    return {"counts": counts, "over_minutes": len(over), "unread": sorted(unread), "capped": sorted(capped)}


ROUTES = {
    ("GET", "/api/logalerts"): ("viewer", lambda request: listing()),
    ("POST", "/api/logalerts"): ("operator", lambda request: save(request.body)),
    ("POST", "/api/logalerts/delete"): ("operator", lambda request: delete((request.body or {}).get("id"))),
    ("POST", "/api/logalerts/preview"): ("viewer", lambda request: preview(request.body)),
}
