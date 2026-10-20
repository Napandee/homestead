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
import os
import threading
from unittest import mock


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


def _job_file(ops, path):
    return (os.path.dirname(os.path.abspath(path)) == os.path.abspath(ops.DATA_DIR)
            and os.path.basename(path) in (ops.STORE, ops.LEGACY_STORE))


@contextlib.contextmanager
def store_writes(ops, fail=None):
    """Watch - or break - the real store's saves where they reach the disk
    (homestead_shared.write_json of a job file in ops.DATA_DIR), rather than
    patching its private _write. Yields the job lists written. fail is an
    exception (instance or class) to raise instead of each write, or a function
    that may raise. A function gets one file's list at a time, as written: after
    pruning, without the _legacy_store marks, and again for the legacy file
    when that changes too."""
    real = ops.SHARED.write_json

    def write_json(path, value, *args, **kwargs):
        if _job_file(ops, path):
            if isinstance(fail, BaseException) or (isinstance(fail, type) and issubclass(fail, BaseException)):
                raise fail
            if fail is not None:
                fail(value)
            written.append(copy.deepcopy(value))
        return real(path, value, *args, **kwargs)

    written = []
    with mock.patch.object(ops.SHARED, "write_json", write_json):
        yield written


@contextlib.contextmanager
def damaged_store(ops):
    """The real store's file made unreadable for the block, then put back as it
    was: reads raise as they do on a damaged disk, rather than patching _read."""
    path = os.path.join(ops.DATA_DIR, ops.STORE)
    try:
        with open(path, "rb") as handle:
            saved = handle.read()
    except FileNotFoundError:
        saved = None
    os.makedirs(ops.DATA_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{damaged")
    try:
        yield
    finally:
        if saved is None:
            with contextlib.suppress(FileNotFoundError):
                os.remove(path)
        else:
            with open(path, "wb") as handle:
                handle.write(saved)
