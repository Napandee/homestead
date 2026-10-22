"""/readyz (#736): whether this Homestead can do its work, in one bit.

Open like /healthz, because kube probes and outside uptime checkers cannot
sign in, so it says nothing but {"ready": true} (200) or {"ready": false}
(503). Which check failed is in /metrics (homestead_metrics, behind a key).

Ready means the Kubernetes API answered GET /version within the last 30 s
and /data took a small write. Both checks are cached: at most one API call
per 10 s and one write per 30 s per replica, run by one caller at a time,
whoever else asks meanwhile gets the last result. So a burst of probes, or
someone hostile, cannot load the API server or the volume. Until both have
run once the answer is no.

The self-data fence answers /readyz in server.py's guard exactly as it
answers /healthz (homestead_web_paths.handoff_body). The probe write itself
goes through the fence and the activity barrier too (homestead_shared's
write scope and guard, as every other /data writer does): while the fence
holds, or a freeze is draining activity, nothing is written and the answer
is not ready. /metrics reads the last /data result and never writes.
"""
import os
import re
import threading
import time

import homestead_kube as KUBE
import homestead_routes as MODULE_ROUTES
import homestead_shared as SHARED
from homestead_storage_journal import Held

DATA_DIR = os.environ.get("DATA_DIR", "/data")
# One probe file per pod, so replicas sharing /data never race on it.
POD_NAME = re.sub(r"[^A-Za-z0-9._-]", "", os.environ.get("HOSTNAME", "")) or "homestead"

API_EVERY, API_FRESH, API_TIMEOUT = 10, 30, 2
DATA_EVERY = 30

_clock = time.monotonic
_lock = threading.Lock()
# None: not checked yet.
START = {"api_checked": None, "api_ok_at": None, "api_seconds": None, "data_checked": None, "data_ok": None}
_state = dict(START)


def _write_probe():
    """Write, flush to disk and remove a small file in /data, inside the
    self-data activity barrier and past the fence; Held when either refuses."""
    path = os.path.join(DATA_DIR, f".readyz-{POD_NAME}")
    with SHARED.write_scope(path):
        if SHARED.WRITE_GUARD is not None:
            SHARED.WRITE_GUARD(path)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.write(fd, b"ready\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.remove(path)
        except OSError:
            pass    # written is what was asked; the next probe truncates it


def _due(checked, every, now):
    return checked is None or now - checked >= every


def _refresh(now, probe_data):
    """Run whichever check is due, unless another caller is running them."""
    if not _lock.acquire(blocking=False):
        return
    try:
        if _due(_state["api_checked"], API_EVERY, now):
            _state["api_checked"] = now
            started = _clock()
            try:
                KUBE.raw_get("/version", timeout=API_TIMEOUT)
                _state.update(api_ok_at=now, api_seconds=max(0.0, _clock() - started))
            except Exception:
                pass
        if probe_data and _due(_state["data_checked"], DATA_EVERY, now):
            _state["data_checked"] = now
            try:
                _write_probe()
                _state["data_ok"] = True
            except (OSError, Held):
                _state["data_ok"] = False
    finally:
        _lock.release()


def state(probe_data=True):
    """The answer and the detail behind it: each check (data_writable None
    until /readyz has probed it), and how long the API last took to answer
    (seconds, None before it ever has). probe_data=False checks the API
    only and reports the last /data result: what /metrics asks."""
    now = _clock()
    _refresh(now, probe_data)
    api = _state["api_ok_at"] is not None and now - _state["api_ok_at"] <= API_FRESH
    data = _state["data_ok"]
    return {"ready": api and data is True, "checks": {"kube_api": api, "data_writable": data},
            "kube_api_seconds": _state["api_seconds"]}


def answer(request=None):
    ready = state()["ready"]
    return MODULE_ROUTES.Reply(200 if ready else 503, {"ready": ready})


# Public (homestead_web_paths.PUBLIC): the role is never asked for.
ROUTES = {("GET", "/readyz"): ("viewer", answer)}
