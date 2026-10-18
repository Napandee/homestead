"""What each kind of job is, in one record (#622).

A kind is spread over the job store's registries today - how it advances
(RESOLVERS), what cancelling it does (CANCELLERS), whether a failed one can
carry on (RESUMABLE), where its output is (LOGGERS), whether cancelling also
cleans up after a failure (CLEANUPS, CDI_CANCELLABLE). homestead_operations
seeds some resolvers itself; server.py, homestead_cancel.register and
homestead_joblogs.register fill most of the rest, and requests, priority,
promote, set_restart and unraid_vms add their own, at import or in bind().
kinds() reads them as they stand on each call, so nothing has to
move for it to be right; the registrations and homestead_operations' kind
branches move into these records later, a few kinds at a time."""
from dataclasses import dataclass
from typing import Callable, Optional

import homestead_operations as OPS


def _no_rules_of_its_own(items, kind, ref, resource):
    """start() still holds every conflict rule, the shared ones and those
    for one kind; a kind's own rules move here later."""
    return None


@dataclass(frozen=True)
class Kind:
    name: str
    resolve: Optional[Callable] = None        # item -> (status, progress, message)
    cancel: Optional[tuple] = None            # (plan(item), run(item, options))
    resumable: Optional[Callable] = None      # item -> why it cannot carry on, or ""
    log: Optional[Callable] = None            # item -> [{"title", "text", "note"}]
    cleanup: bool = False                     # cancelling a failed one tidies up after it
    cdi_cancellable: bool = False             # cancellable while its CDI receipt is held
    view: Callable = OPS._public              # item -> what the job tray is sent
    conflicts: Callable = _no_rules_of_its_own


def kinds(ops=OPS):
    """Every kind any registry names, by name. A kind no resolver advances
    still shows, with resolve None: that is a half-registered kind."""
    names = (set(ops.RESOLVERS) | set(ops.CANCELLERS) | set(ops.RESUMABLE) | set(ops.LOGGERS)
             | set(ops.CLEANUPS) | set(ops.CDI_CANCELLABLE))
    return {name: Kind(name, resolve=ops.RESOLVERS.get(name), cancel=ops.CANCELLERS.get(name),
                       resumable=ops.RESUMABLE.get(name), log=ops.LOGGERS.get(name),
                       cleanup=name in ops.CLEANUPS, cdi_cancellable=name in ops.CDI_CANCELLABLE,
                       view=ops._public)
            for name in sorted(names)}


def kind(name, ops=OPS):
    """One kind; KeyError when no registry names it."""
    return kinds(ops)[name]
