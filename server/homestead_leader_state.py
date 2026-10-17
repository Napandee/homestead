"""What the leader did, for every replica to show.

Work that must happen once runs on the leader (homestead_leader), but any
replica answers the page. What that work last did - the MQTT publisher's
state, each background task's last beat, the last tidy, the last outage
webhook - is kept in the leader's memory, so a page another replica answered
said nothing, or "standby". The leader also writes it here, on the shared
data volume every replica mounts, and a replica that is not leading shows
that while it is fresh.

One small JSON file, a section per subject, each with when it was written.
A section is written again when its key changes, and otherwise at most once
every few seconds the caller chooses, so a loop that beats often does not
write as often.
"""
import json
import os
import threading
import time

import homestead_shared as SHARED

FILE = "leader-state.json"
_lock = SHARED.SharedLock("leader-state")
_written = {}            # section -> (when, key) of this replica's last write
_written_lock = threading.Lock()
_failing = set()         # sections whose last write failed: said once, not every beat


def _path():
    return os.path.join(SHARED.DIR, FILE)


def _load():
    try:
        with open(_path(), encoding="utf-8") as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def put(section, value, *, every=0, key=None, now=None):
    """The leader's value for section. Skipped while key is unchanged and
    the last write is under every seconds old. Never raises: what is shown
    elsewhere is not worth stopping the work for."""
    now = now or time.time()
    with _written_lock:
        last = _written.get(section)
        if last and last[1] == key and now - last[0] < every:
            return False
        _written[section] = (now, key)
    try:
        with _lock:
            state = _load()
            state[section] = {"at": int(now), "value": value}
            SHARED.write_json(_path(), state, separators=(",", ":"))
        _failing.discard(section)
        return True
    except Exception as error:
        # During a move of Homestead's own data, writes are held: tried again next time.
        with _written_lock:
            _written.pop(section, None)
        if section not in _failing:
            _failing.add(section)
            print(f"leader state: {section} not shared: {str(error)[:160]}", flush=True)
        return False


def get(section, max_age=None, now=None):
    """(value, when written) as the leader last wrote it; (None, 0) when it
    has not, or not within max_age seconds."""
    now = now or time.time()
    found = _load().get(section)
    if not isinstance(found, dict) or "value" not in found:
        return None, 0
    at = int(found.get("at") or 0)
    if max_age is not None and now - at > max_age:
        return None, 0
    return found["value"], at
