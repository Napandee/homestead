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
# Refused requests with no user: at most one line per address a minute, so a
# client retrying cannot wash the changes out of the log.
REFUSED = (401, 403, 429)
REFUSAL_SECONDS = 60
REFUSAL_ADDRESSES = 1000

# On the data volume as well, so the record survives a restart without a log
# collector: two files of at most MAX_BYTES, ~60 000 lines.
FILE = "audit.jsonl"
LOCK = "audit.lock"
MAX_BYTES = 10 * 1024 * 1024

_refusals = {}
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


def line(request, status, ms, notes, now=None):
    """The line for one finished request, or None when it gets none."""
    if request.path == MCP_PATH and notes.get("kind") not in CHANGE_TOOLS:
        return None             # initialize, tools/list, ping and read tools are reads
    conn = request.conn
    at = datetime.datetime.fromtimestamp(time.time() if now is None else now, datetime.timezone.utc)
    entry = {"v": 1, "at": at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
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


def _throttled(entry):
    if entry["user"] or entry["status"] not in REFUSED:
        return False
    now, addr = time.time(), entry["addr"]
    if now - _refusals.get(addr, 0) <= REFUSAL_SECONDS:
        return True
    if len(_refusals) >= REFUSAL_ADDRESSES:
        _refusals.clear()           # many addresses at once: start again
    _refusals[addr] = now
    return False


def record(request, status, ms):
    """The hook: one line for a finished change request. Never raises."""
    try:
        notes = _take_notes()
        if not audited(request.method, request.path):
            return
        entry = line(request, status, ms, notes)
        if entry is not None and not _throttled(entry):
            emit(entry)
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
        if size + incoming > MAX_BYTES:
            os.replace(path, path + ".1")
    finally:
        os.close(fd)


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


def emit(entry):
    """Write a line: to stdout, for a log collector, and to the data volume."""
    line_text = text(entry)
    _stdout(line_text)
    if _file_on():
        _append(line_text)


MODULE_ROUTES.after(record, fields=MODULE_ROUTES.HOOK_FIELDS)
