"""One audit line per change request (#738).

What a person did through Homestead was spread over the change history, the
agent audit, the sign-in history and an annotation that keeps only the last
writer, and none of it reached the pod log. This writes one JSON line for every
request that changes something: who, how they came in, what, the target, the
status and how long it took - after the answer, wherever it was answered.

The line is built from what homestead_routes.after_request hands a hook: no
body but the named fields ns, namespace, kind and name (checked and redacted
there), no query, no cookies, no secret header and never the answer. So no
password, setup code, key or cookie can reach it. What that view cannot know -
the linked cluster a request was relayed to, an AI agent's tool - is noted
during the request with note(), and taken by the hook.
"""
import collections
import datetime
import json
import os
import re
import sys
import threading
import time

import homestead_routes as MODULE_ROUTES
import homestead_shared as SHARED

try:
    import fcntl
except ImportError:
    fcntl = None

CHANGE = ("POST", "PUT", "PATCH", "DELETE")
# Opening a shell or a console is what people ask "who did that" about; one
# line when the session ends, with its length.
CONSOLES = ("/api/console", "/api/node/shell", "/api/vm/console")
MCP_PATH = "/mcp"
NOT_AUDITED = {
    ("POST", "/api/auth/login"): "the sign-in history records it, and a failed sign-in has no user",
    ("POST", "/api/auth/preferences/dashboard"): "a person's own dashboard layout, not the cluster",
}
# What a request may note for its line; the MCP tool's class decides whether
# a call to /mcp is a change, and is not written itself.
NOTE_KEYS = ("to", "tool", "kind", "held", "outcome")
CHANGE_TOOLS = ("change", "destructive")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
NOTE_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}")     # tool names have underscores
EMAIL = re.compile(r"[^\s@]{1,64}@[^\s@]{1,189}")
DELETE_WORKLOAD = re.compile(r"/api/workload/([a-z0-9.-]+)/([a-z0-9.-]+)")
PATH_CAP = 200
# A flood of refused requests must not wash real changes out of the two files
# on the data volume. stdout carries every line; the file keeps every success
# (anonymous ones too: first-admin setup) and every 5xx, and of 4xx refusals,
# each WINDOW_SECONDS, at most BURST per address and per user, then ALL_BURST in
# all - one ceiling for refusals with no user, another for signed-in people -
# and always the first refusal from each address or user. What it leaves out is
# summed up in one line. The table is bounded (least recently used goes first,
# its summary written), never cleared.
WINDOW_SECONDS = 60
BURST = 5
ALL_BURST = 120
KEYS_KEPT = 1000
SWEEP_SECONDS = 5
# Two rotations closer than this say something is filling the file.
ROTATION_ALARM_SECONDS = 3600
# On the data volume as well, so the record survives a restart without a log
# collector: two files of at most MAX_BYTES, ~60 000 lines.
FILE = "audit.jsonl"
LOCK = "audit.lock"
MAX_BYTES = 10 * 1024 * 1024
_buckets = collections.OrderedDict()   # ("addr"|"user"|"all", name) -> window
_swept = [0.0]
_rotated = []
_throttle = threading.Lock()
_notes = threading.local()
_lock = threading.Lock()


def audited(method, path):
    """Whether a request to path with method gets a line."""
    if (method, path) in NOT_AUDITED:
        return False
    if method == "GET":
        return path in CONSOLES
    return method in CHANGE and (path.startswith("/api/") or path == MCP_PATH)


def note(**fields):
    """Something this request's line should say that the hook cannot see."""
    unknown = sorted(set(fields) - set(NOTE_KEYS))
    if unknown:
        raise ValueError(f"an audit note may say only {', '.join(NOTE_KEYS)}, not {', '.join(unknown)}")
    _notes.__dict__.update({key: str(value) for key, value in fields.items()})


def _take_notes():
    taken = dict(_notes.__dict__)
    _notes.__dict__.clear()
    return taken


def _name(value):
    return value if isinstance(value, str) and NAME.fullmatch(value) else None


def _target(request):
    body = request.body if isinstance(request.body, dict) else {}
    found = {"ns": _name(body.get("ns")) or _name(body.get("namespace")),
             "kind": _name(body.get("kind")), "name": _name(body.get("name"))}
    match = request.method == "DELETE" and DELETE_WORKLOAD.fullmatch(request.path)
    if match:
        found.update(ns=match.group(1), name=match.group(2))
    return {key: value for key, value in found.items() if value}


def _via(request, conn):
    if conn is not None and conn.key:
        return "mcp" if request.path == MCP_PATH else "key"
    if conn is not None and conn.fleet:
        return "linked"
    return "browser" if request.user else "none"


def _now_text(seconds):
    at = datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc)
    return at.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def line(request, status, ms, notes, now=None):
    """The line for one finished request, or None when it gets none."""
    if request.path == MCP_PATH and notes.get("kind") not in CHANGE_TOOLS:
        return None             # initialize, tools/list, ping and read tools are reads
    conn = request.conn
    entry = {"v": 1, "at": _now_text(time.time() if now is None else now),
             "replica": os.environ.get("HOSTNAME", ""), "via": _via(request, conn),
             "user": request.user or "", "role": request.role, "addr": request.addr}
    email = (conn.access or {}).get("email") if conn is not None and isinstance(conn.access, dict) else None
    if isinstance(email, str) and len(email) <= 254 and EMAIL.fullmatch(email):
        entry["access"] = email
    if conn is not None and conn.key:
        entry["key"], entry["owner"] = conn.key.get("id"), conn.key.get("owner")
    if conn is not None and conn.fleet:
        entry["from"] = conn.fleet
    for key in ("to", "tool", "held", "outcome"):
        if isinstance(notes.get(key), str) and NOTE_VALUE.fullmatch(notes[key]):
            entry[key] = notes[key]
    entry.update(method=request.method, path=request.path[:PATH_CAP])
    target = _target(request)
    if target:
        entry["target"] = target
    entry.update(status=status, ms=ms)
    if request.method == "GET" and request.path in CONSOLES:
        entry["session_s"] = round(ms / 1000)
    return entry


def _refusal(entry):
    status = entry["status"]
    return isinstance(status, int) and 400 <= status < 500


def _summary(key, window):
    kind, name = key
    entry = {"v": 1, "at": _now_text(time.time()), "replica": os.environ.get("HOSTNAME", ""),
             "event": "audit-suppressed", "suppressed": window["dropped"], "window_s": WINDOW_SECONDS,
             "since": _now_text(window["start"])}
    if kind == "user":
        entry["user"] = name
        who = f"by {name}"
    elif kind == "addr":
        entry["addr"] = name
        who = f"from {name}"
    elif name == "signed-in":
        entry["user"] = "*"
        who = "by signed-in people"
    else:
        entry["addr"] = "*"
        who = "with no user, from any address"
    entry["detail"] = (f"{window['dropped']} more refused requests {who} "
                       f"in the {WINDOW_SECONDS} s since {entry['since']}: on stdout, not kept here")
    return entry


def _window(key, now, summaries):
    window = _buckets.get(key)
    if window is not None and now - window["start"] >= WINDOW_SECONDS:
        if window["dropped"]:
            summaries.append(_summary(key, window))
        window = None
    if window is None:
        window = _buckets[key] = {"start": now, "kept": 0, "dropped": 0}
    _buckets.move_to_end(key)
    return window


def _keep(entry):
    """Whether the data volume keeps this line, and the summaries due first."""
    now, summaries = time.time(), []
    with _throttle:
        if now - _swept[0] >= SWEEP_SECONDS:
            _swept[0] = now
            for key in [k for k, w in _buckets.items() if now - w["start"] >= WINDOW_SECONDS]:
                window = _buckets.pop(key)
                if window["dropped"]:
                    summaries.append(_summary(key, window))
        if not _refusal(entry):
            return True, summaries
        mine = [_window(("addr", entry["addr"]), now, summaries)]
        if entry["user"]:
            mine.insert(0, _window(("user", entry["user"]), now, summaries))
        everyone = _window(("all", "signed-in" if entry["user"] else "anonymous"), now, summaries)
        while len(_buckets) > KEYS_KEPT:
            key, window = _buckets.popitem(last=False)
            if window["dropped"]:
                summaries.append(_summary(key, window))
        full = next((window for window in mine if window["kept"] >= BURST), None)
        if full is not None:
            full["dropped"] += 1
            return False, summaries
        first = any(window["kept"] == 0 for window in mine)
        if everyone["kept"] >= ALL_BURST and not first:
            everyone["dropped"] += 1
            return False, summaries
        for window in [everyone, *mine]:
            window["kept"] += 1
        return True, summaries


def record(request, status, ms):
    """The hook: one line for a finished change request. Never raises."""
    try:
        notes = _take_notes()
        if not audited(request.method, request.path):
            return
        entry = line(request, status, ms, notes)
        if entry is None:
            return
        keep, summaries = _keep(entry)
        for summary in summaries:
            if _file_on():
                _append(text(summary))
        emit(entry, keep)
    except Exception:
        pass        # recording never changes or breaks an answer


def text(entry):
    """One line of JSON, whatever a path or a name holds."""
    return json.dumps(entry, ensure_ascii=True, separators=(",", ":"))


def _stdout(line_text):
    with _lock:
        sys.stdout.write(line_text + "\n")
        sys.stdout.flush()


SETTING_SECONDS = 30
_setting = {"value": True, "read": None}       # on until a read says otherwise


def _file_on():
    """The audit.file setting, read at most every SETTING_SECONDS. A read that
    fails keeps the last answer: the cluster may be restarting."""
    now = time.monotonic()
    if _setting["read"] is None or now - _setting["read"] >= SETTING_SECONDS:
        _setting["read"] = now
        try:
            import homestead_settings_helpers as SETTINGS
            _setting["value"] = SETTINGS.get_app_settings()["audit"]["file"] is not False
        except Exception:
            pass
    return _setting["value"]


def _rotate(path, incoming):
    """audit.jsonl becomes audit.jsonl.1 (replacing it) when the next line would
    not fit. Under audit.lock, and checked again there, so two replicas that
    both saw a full file rotate it once."""
    fd = os.open(os.path.join(os.path.dirname(path), LOCK),
                 os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            size = os.stat(path).st_size
        except FileNotFoundError:
            return
        if size + incoming <= MAX_BYTES:
            return
        os.replace(path, path + ".1")
    finally:
        os.close(fd)
    now = time.time()
    if _rotated and now - _rotated[-1] < ROTATION_ALARM_SECONDS:
        _stdout(text({"v": 1, "at": _now_text(now), "replica": os.environ.get("HOSTNAME", ""),
                      "event": "audit-file-rotated-quickly",
                      "detail": f"audit.jsonl filled {MAX_BYTES // (1024 * 1024)} MiB twice within an hour, so "
                                "something may be flooding Homestead with requests; the pod log keeps every line"}))
    _rotated[:] = [now]


def _append(line_text):
    """One line onto the data volume, only through the self-data write fence:
    while a data move holds /data nothing is written here, and nothing raises."""
    folder = SHARED.DIR
    if not os.path.isdir(folder):
        return                      # no data volume: never made here
    path, data = os.path.join(folder, FILE), (line_text + "\n").encode("ascii")
    try:
        with SHARED.write_scope(path):
            if SHARED.WRITE_GUARD is not None:
                SHARED.WRITE_GUARD(path)
            try:
                size = os.stat(path).st_size
            except FileNotFoundError:
                size = 0
            if size + len(data) > MAX_BYTES:
                _rotate(path, len(data))
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
    except Exception:
        pass                        # held, full or unwritable: stdout has the line


def emit(entry, keep=True):
    """Write a line: always to stdout, for a log collector, and to the data
    volume unless the throttle leaves it out (keep False)."""
    line_text = text(entry)
    _stdout(line_text)
    if keep and _file_on():
        _append(line_text)


MODULE_ROUTES.after(record, fields=MODULE_ROUTES.HOOK_FIELDS)
