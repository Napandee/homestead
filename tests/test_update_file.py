"""Stores on the shared data volume are changed under a lock every copy of
Homestead takes, so two copies cannot each save an old copy (#621)."""
import fcntl
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_agent_requests as REQUESTS
import homestead_api_keys as KEYS
import homestead_shared as SHARED


class OtherCopy:
    """Another copy of Homestead holding a lock: its own open file and flock."""
    def __init__(self, directory, name):
        os.makedirs(os.path.join(directory, ".locks"), exist_ok=True)
        self.handle = open(os.path.join(directory, ".locks", name + ".lock"), "a+")

    def __enter__(self):
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()


def waits_for(test, other, action):
    """action does not finish while the other copy holds its lock, and does after."""
    done = []
    with other:
        thread = threading.Thread(target=lambda: done.append(action()))
        thread.start()
        thread.join(0.4)
        test.assertEqual([], done, "it went ahead while another copy held the lock")
    thread.join(5)
    test.assertEqual(1, len(done))
    return done[0]


class Folder(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def path(self, name):
        return os.path.join(self.dir.name, name)

    def stored(self, name):
        with open(self.path(name), encoding="utf-8") as handle:
            return json.load(handle)


class UpdateFileTests(Folder):
    def setUp(self):
        super().setUp()
        self.lock = SHARED.SharedLock("store", directory=lambda: self.dir.name)

    def test_the_change_is_made_on_what_is_stored_and_saved(self):
        SHARED.write_json(self.path("store.json"), {"a": 1})
        result = SHARED.update_file(self.path("store.json"), lambda value: value.update(b=2) or "done", self.lock, {})
        self.assertEqual("done", result)
        self.assertEqual({"a": 1, "b": 2}, self.stored("store.json"))

    def test_a_missing_store_starts_from_the_default(self):
        SHARED.update_file(self.path("store.json"), lambda rows: rows.append(1), self.lock, [])
        self.assertEqual([1], self.stored("store.json"))

    def test_a_change_that_raises_saves_nothing(self):
        SHARED.write_json(self.path("store.json"), {"a": 1})
        def half(value):
            value["a"] = 2
            raise ValueError("stopped")
        with self.assertRaises(ValueError):
            SHARED.update_file(self.path("store.json"), half, self.lock, {})
        self.assertEqual({"a": 1}, self.stored("store.json"))

    def test_an_unreadable_store_is_not_saved_over(self):
        with open(self.path("store.json"), "w", encoding="utf-8") as handle:
            handle.write("{half")
        with self.assertRaises(SHARED.Unreadable):
            SHARED.update_file(self.path("store.json"), lambda value: value.update(b=2), self.lock, {})

    def test_it_waits_for_another_copy_holding_the_lock(self):
        waits_for(self, OtherCopy(self.dir.name, "store"),
                  lambda: SHARED.update_file(self.path("store.json"), lambda value: value.update(b=2), self.lock, {}))
        self.assertEqual({"b": 2}, self.stored("store.json"))


class AgentRequestTests(Folder):
    def setUp(self):
        super().setUp()
        REQUESTS.bind(self.dir.name)

    def test_filing_a_request_waits_for_another_copy(self):
        tool = types.SimpleNamespace(name="restart_app", title="Restart an app")
        key = {"id": "k1", "name": "assistant", "owner": "ada"}
        row = waits_for(self, OtherCopy(self.dir.name, "agent-requests"),
                        lambda: REQUESTS.create(tool, {"name": "web"}, {"action": "Restart web"}, "operator", key))
        self.assertEqual("pending", self.stored(REQUESTS.FILE)[row["id"]]["status"])


class KeyUsageTests(Folder):
    def setUp(self):
        super().setUp()
        KEYS.bind(self.dir.name)
        KEYS._usage_written.clear()

    def test_noting_a_use_waits_for_another_copy_and_keeps_its_entries(self):
        SHARED.write_json(self.path("api-key-usage.json"), {"k0": {"at": 1, "ip": "192.0.2.9"}})
        waits_for(self, OtherCopy(self.dir.name, "api-key-usage"),
                  lambda: KEYS._note_use("k1", "192.0.2.10", 10_000))
        self.assertEqual({"k0": {"at": 1, "ip": "192.0.2.9"}, "k1": {"at": 10000, "ip": "192.0.2.10"}},
                         self.stored("api-key-usage.json"))


if __name__ == "__main__":
    unittest.main()
