import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes.job_store import Store, put


class PutTests(unittest.TestCase):
    def store(self, items):
        saved = []
        store = Store(read=lambda: [dict(i) for i in items], write=lambda rows: saved.append((store._lock._is_owned(), rows)))
        return store, saved

    def test_put_saves_the_items_given_under_the_lock(self):
        store, saved = self.store([{"id": "a"}])
        put(store, [{"id": "b"}])
        self.assertEqual([(True, [{"id": "b"}])], saved)

    def test_put_without_items_saves_the_jobs_as_stored(self):
        store, saved = self.store([{"id": "a"}])
        put(store)
        self.assertEqual([(True, [{"id": "a"}])], saved)

    def test_a_put_that_fails_releases_the_lock(self):
        store = Store(read=list, write=lambda rows: (_ for _ in ()).throw(OSError("lost storage")))
        with self.assertRaises(OSError):
            put(store, [])
        self.assertFalse(store._lock._is_owned())


if __name__ == "__main__":
    unittest.main()
