"""The leader's page snapshot (#705 step 4b). One cycle on the leader reads
the cluster once for the Dashboard, Hosts, Apps, Volumes and storage pages:
the pod list and Longhorn's volumes are read once and shared, and each page's
part is kept here and in homestead_cache under its route's key. The routes
serve a part through homestead_routes.page() while it is fresh; otherwise
they compute as before.

A part is kept with as_of, when its cycle started reading, so it is never
dated newer than the cluster state it holds. A part read before a write that
forgets its key (homestead_kube.fresh_reads, homestead_routes.forget) is not
kept, nor served after it; the forget also wakes the cycle.

homestead_overview_server's _sampler drives the cycle on the leader and
passes the builders in, so this module imports none of the page modules."""
import threading
import time

import homestead_cache as CACHE
import homestead_routes as ROUTER

# The parts, by their routes' cache keys.
PARTS = ("nodes", "wl", "vol", "stor", "lhcap", "ov")
# A part older than this is not served: three missed cycles.
STALE_AFTER = 60
# A woken cycle starts no sooner than this after the last one started, so a
# burst of writes does not run cycles back to back.
MIN_GAP = 2
# How often the loop looks at leadership and wakes between cycles.
LEADER_CHECK = 2

_lock = threading.Lock()
_parts = {}             # key -> (as_of, value)
_wake = threading.Event()
_wanted = threading.Event()
_state = {"last": None, "leader": False}


def cadence():
    """Seconds between cycles: the pages' own cache time, so no number is
    older than it was before the snapshot."""
    return ROUTER.PAGE_SECONDS


def reset():
    """Forget every part and the loop's state (tests, and a lost lease)."""
    with _lock:
        _parts.clear()
    _wanted.clear()
    _state.update(last=None, leader=False)


def wake():
    """Run a cycle soon rather than at its next turn."""
    _wanted.set()
    _wake.set()


def _forgotten(keys):
    if set(keys) & set(PARTS):
        wake()


CACHE.on_forget(_forgotten)


def read(key, now=None):
    """(as_of, value) for key, or None when there is no part, it is older than
    STALE_AFTER, key was forgotten after its cycle started reading, or the
    part is no longer what the cache holds under key: a module that pops its
    keys after a write (homestead_hardware, homestead_storage_classes, ...)
    or a newer read of key wins over it."""
    with _lock:
        part = _parts.get(key)
    if part is None:
        return None
    now = time.time() if now is None else now
    if now - part[0] >= STALE_AFTER or part[0] <= CACHE.forgotten_at(key) or CACHE.kept_at(key) != part[0]:
        return None
    return part


ROUTER.use_snapshot(read)


def cycle(inputs, builders):
    """Read each input once (inputs: {name: fn()}), then build each part in
    order (builders: [(key, fn(got))], got holding the inputs and the parts
    built so far). A part that fails, or whose input failed, keeps its last
    value and as_of. Returns {key: True when kept, False when it failed,
    None when a write overtook it (forgotten while it was read)}."""
    epochs = {key: CACHE.epoch(key) for key, _ in builders}
    as_of = time.time()
    got, done = {}, {}
    for name, fn in inputs.items():
        try:
            got[name] = fn()
        except Exception as error:
            print(f"page snapshot: {name} not read: {str(error)[:160]}", flush=True)
    for key, fn in builders:
        try:
            value = fn(got)
        except Exception as error:
            print(f"page snapshot: {key} not built: {str(error)[:160]}", flush=True)
            done[key] = False
            continue
        got[key] = value
        with _lock:
            if CACHE.keep(key, value, epochs[key], at=as_of):
                done[key] = True
                _parts[key] = (as_of, value)
            else:
                done[key] = None
    return done


def tick(is_leader, inputs, builders, now=None):
    """One turn of the loop: on the leader, a cycle when one is due (every
    cadence(), at once on boot and on becoming the leader, and soon after a
    wake). Returns what cycle() returned, or None when none ran."""
    leader = bool(is_leader())
    became = leader and not _state["leader"]
    if not leader:
        if _state["leader"]:
            # A replica that lost the lease no longer refreshes its parts:
            # it computes for itself, as a standby does.
            with _lock:
                _parts.clear()
        _state["leader"] = False
        return None
    _state["leader"] = True
    now = time.monotonic() if now is None else now
    last = _state["last"]
    due = (last is None or became or now - last >= cadence()
           or (_wanted.is_set() and now - last >= MIN_GAP))
    if not due:
        return None
    _wanted.clear()
    _state["last"] = now
    return cycle(inputs, builders)


def sleep(seconds):
    """Wait up to seconds, or until something wakes the loop."""
    _wake.wait(seconds)
    _wake.clear()
