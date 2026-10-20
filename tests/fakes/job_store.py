"""stored() and transaction() for stand-ins of homestead_operations (#622).

A stand-in keeps its jobs its own way and gives this mixin a re-entrant
_lock, _read() and, if anything saves, _write(items). The two calls then
behave as the real ones do: stored() returns a fresh copy, as reading the file
does, so a change nobody saves is lost; transaction() holds the lock for its
block and writes only when the block calls save().

    class Operations(JobStore):
        def __init__(self): self._lock = threading.RLock(); self.items = []
        def _read(self): return self.items

    Store(read=lambda: copy.deepcopy(saved), write=keep, require_write=lambda: None)
"""
import contextlib
import copy
import threading


class Transaction:
    def __init__(self, store):
        self._store, self._items = store, None

    @property
    def items(self):
        if self._items is None:
            self._items = self._store._read()
        return self._items

    def save(self, items=None):
        if items is not None:
            self._items = items
        self._store._write(self.items)


class JobStore:
    def stored(self):
        return copy.deepcopy(self._read())

    @contextlib.contextmanager
    def transaction(self):
        with self._lock:
            yield Transaction(self)


def put(ops, items=None):
    """Save items - or, given none, the jobs as stored - through ops.transaction(),
    as the code does; ops is homestead_operations or one of the stand-ins here."""
    with ops.transaction() as tx:
        tx.save(items)


class Store(JobStore):
    """A stand-in built from a read and an optional write, plus any other names the code calls."""
    def __init__(self, read=list, write=None, **names):
        self._lock, self._read, self._write = threading.RLock(), read, write
        self.__dict__.update(names)
