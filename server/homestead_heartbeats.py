"""Each background task says when it last did its work, and what went wrong
if it did not, so Settings > Homestead can say whether Homestead is healthy
rather than only whether it answers.

A task that runs on the leader only beats there; the leader shares those
beats (homestead_leader_state) so a page another replica answers shows them
too, instead of "standby" for every one.
"""
import threading
import time

import homestead_leader_state as LEADER_STATE

HEART = {}
_heart_lock = threading.Lock()
SHARE_EVERY = 30


def heartbeats():
    """Each background task's last beat, copied under the lock."""
    with _heart_lock:
        return {k: dict(v) for k, v in HEART.items()}


def beat(name, every, error=None, leader_only=False):
    now = time.time()
    with _heart_lock:
        row = HEART.setdefault(name, {"every": every, "leader_only": leader_only, "last_ok": 0, "error": "", "error_at": 0})
        row["seen"] = now
        if error is None:
            row["last_ok"] = now
            row["error"] = ""
        else:
            row["error"], row["error_at"] = str(error)[:200], now
        shared = {k: dict(v) for k, v in HEART.items() if v.get("leader_only")} if leader_only else None
    if shared is not None:
        # Written at once when a task starts or stops failing, else every SHARE_EVERY seconds.
        LEADER_STATE.put("heartbeats", shared, every=SHARE_EVERY,
                         key=tuple(sorted((k, bool(v["error"])) for k, v in shared.items())), now=now)


def leader_beats(max_age=600):
    """The leader's beats for its leader-only tasks, while fresh; {} if not."""
    value, _ = LEADER_STATE.get("heartbeats", max_age=max_age)
    return value if isinstance(value, dict) else {}
