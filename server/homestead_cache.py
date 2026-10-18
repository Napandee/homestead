"""Homestead's short-lived answers: each read of the cluster kept for a few
seconds under a key, so a page and its refresh, or two pages at once, ask
the API server once. server.py keeps the same dict and lock as _cache and
_lock, and hands them to the modules that read or clear entries.
Tests patch cached here."""
import threading
import time

_cache = {}
_lock = threading.Lock()


def cached(key, ttl, fn):
    with _lock:
        e = _cache.get(key)
        if e and time.time() - e[0] < ttl:
            return e[1]
    try:
        v = fn()
    except Exception as ex:
        with _lock:
            e = _cache.get(key)
        if e:
            return e[1]
        raise ex
    with _lock:
        _cache[key] = (time.time(), v)
    return v
