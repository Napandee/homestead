"""The destination half of a move, and the part that drives it.

A move is a sequence of phases, each one a question with a yes-or-not-yet
answer: are both clusters writing to the same bucket, has the workload stopped
over there, are its backups done, can this cluster's Longhorn see them, have
the claims been restored, does the workload exist here, is it running.

The engine asks the current phase's question every few seconds and moves on
when the answer is yes. Nothing waits in memory: the move's state is written to
Homestead's own volume after every step, and every step is safe to repeat, so
a restart part-way through - of this Homestead or the other one - picks up
exactly where it was. Data moves take as long as data takes; nothing here has
a timeout that would abandon a large volume halfway.

Split by responsibility (#623): the bound clients, records and shared helpers
live in homestead_move_state, borrowing the backup target in
homestead_move_target, reviewing and starting a move in homestead_move_plan,
the phases and the tick in homestead_move_phases, and retry, abandon, dismiss
and finish in homestead_move_actions. This module keeps bind(), after_finish
(server.py sets it here), the routes and the Activity tray registration, and
re-exports every moved name except the bound clients for one release. Replace
a name in the module that defines it: the bound clients (kget, ksend, LH,
CLIENT, NETWORK, OPS, DATA_DIR, NS) are only on homestead_move_state.
"""
import homestead_move as MOVE
import homestead_names as NAMES
import homestead_move_state as STATE
import homestead_move_plan as PLAN
import homestead_move_actions as ACT


# server.py's check that an app is not one Homestead manages from elsewhere
# (Network Shares), before a move of it starts.
guard_managed_smb = lambda ns, name: None


def bind(_kget, _ksend, longhorn, client, network, operations, data_dir, namespace, guard=None):
    global guard_managed_smb
    STATE.kget, STATE.ksend, STATE.LH, STATE.CLIENT, STATE.NETWORK, STATE.OPS = \
        _kget, _ksend, longhorn, client, network, operations
    STATE.DATA_DIR, STATE.NS = data_dir, namespace
    if guard:
        guard_managed_smb = guard
    NAMES.bind(_kget)


# Called after a move finishes to tidy safe restore metadata.
after_finish = None


def _plan_or_start(request, starting):
    """Review a move, or start it, from what the request names."""
    b = request.body
    call = PLAN.start if starting else PLAN.plan
    if starting and (b.get("kind") or "container") == "container":
        guard_managed_smb(b.get("namespace") or STATE.NS, b.get("name"))
    return MOVE.answer(lambda: call(
        b.get("cluster"), b.get("kind") or "container", b.get("name"),
        b.get("namespace") or STATE.NS, b.get("address_mode") or "shared",
        b.get("address") or "", b.get("storage_class") or "",
        b.get("volumes") if isinstance(b.get("volumes"), dict) else None,
        b.get("transfer_mode") or "move", b.get("source_namespace") or "", b.get("host_devices")))


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/move/moves"): ("viewer", lambda request: STATE.moves()),
    ("POST", "/api/move/moves/dismiss"): ("admin", lambda request: ACT.dismiss(request.body.get("id") or None)),
    ("POST", "/api/move/plan"): ("admin", lambda request: _plan_or_start(request, False)),
    ("POST", "/api/move/start"): ("admin", lambda request: _plan_or_start(request, True)),
    ("POST", "/api/move/moves/retry"): ("admin", lambda request: MOVE.answer(lambda: ACT.retry(request.body.get("id")))),
    ("POST", "/api/move/moves/abandon"): ("admin", lambda request: MOVE.answer(lambda: ACT.abandon(request.body.get("id")))),
    ("POST", "/api/move/moves/finish"): ("admin", lambda request: MOVE.answer(
        lambda: ACT.finish(request.body.get("id"), bool(request.body.get("volumes"))))),
}


def register(ops):
    """A move reads in the Activity tray like every other long job (#622)."""
    ops.RESOLVERS["move"] = PLAN.op_state


# Every moved name, for one release (#623).
from homestead_move_state import (  # noqa: E402,F401
    STORE, LHNS, LH_API, JOIN_SECRET, VIP_ANNOTATION, HARVESTER_IMAGE_CLASS, MOVE_ID,
    MOVED_FROM, TICK_SECONDS, MAX_TRANSIENT, START_GRACE, MAX_MOVES, PHASES, VOLUME_PHASES,
    COPY_PHASES, KINDS, _lock, _step_lock, _step_locks, _step_locks_guard, _serialized, _now,
    _path, _read, _write, _find, _store, _update, _public, _phases, _source_held, moves, _q,
    _here_endpoint, _same_target, _definition, _source_status, _source_action, _object_path,
    _collection_path, _ours, _service_ports, _plan_service, _moving, _note, _advance,
)
from homestead_move_target import (  # noqa: E402,F401
    _target_lock, TARGET_STORE, _target_journal, _borrow_target, _borrow_target_locked,
    _release_target, _cleanup_target,
)
from homestead_move_plan import (  # noqa: E402,F401
    ACTIONS, _all_classes, _choices, storage_choices, plan, start, _add_operation,
    _refuse_second, _operation, op_state,
)
from homestead_move_phases import (  # noqa: E402,F401
    _joining, _quiescing, _backing_up, _releasing_source, _request_sync, _syncing,
    _ensure_namespace, _image_ready, _image_class, _ensure_image, _restore_progress, _restoring,
    _blank_claim, _post_ours, _stamp, carry_icon, _creating, _starting, HANDLERS, _finish,
    _cleanup_resources, _kubernetes_reason, _tick, tick_all, _tick_all, _remove_created,
)
from homestead_move_actions import (  # noqa: E402,F401
    retry, _retry, abandon, _abandon, DISMISSABLE, dismiss, finish, _remove_source,
)
