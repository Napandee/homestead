"""List helpers that tolerate a missing list (#619 step 4, second batch).

firewall, lhrebuild, uplinks and container_rebalance read a list as empty when
it is not there (404; firewall only when asked) or has no "items", and keep
the rows of a list cut short. Changing that is the strict-reads issue (#620):
these tests pin that the move onto homestead_kube.get_optional keeps it, that
every read goes through the module's own reader, and that any other failure
still raises."""
import io
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_container_rebalance as CREBALANCE
import homestead_firewall as FIREWALL
import homestead_kube as KUBE
import homestead_lhrebuild as LHREBUILD
import homestead_uplinks as UPLINKS


def http_error(code):
    return urllib.error.HTTPError("/x", code, "error", {}, io.BytesIO(b""))


class Reads:
    """A reader answering from a dict of path -> object or exception."""
    def __init__(self, answers):
        self.answers, self.paths = answers, []

    def __call__(self, path, **_):
        self.paths.append(path)
        answer = self.answers[path]
        if isinstance(answer, Exception):
            raise answer
        return answer


PATH = "/api/v1/nodes"
ROW = {"metadata": {"name": "a"}}
CUT_SHORT = {"items": [ROW], "metadata": {"continue": "token"}}


class Tolerant:
    """What the four helpers share; each test class says how to call its own."""
    MODULE = None

    def listing(self, answer, **options):
        reads = Reads({PATH: answer})
        # A read through the real client (not the module's reader) fails with a KeyError.
        with mock.patch.object(self.MODULE, "kget", reads), mock.patch.object(KUBE, "kget", Reads({})):
            rows = self.MODULE._items(PATH, **options)
        self.assertEqual(reads.paths, [PATH])
        return rows

    def test_rows_come_through_the_module_reader(self):
        self.assertEqual(self.listing({"items": [ROW]}), [ROW])

    def test_a_list_without_items_is_empty(self):
        self.assertEqual(self.listing({}), [])

    def test_a_list_cut_short_keeps_its_rows(self):
        self.assertEqual(self.listing(CUT_SHORT), [ROW])

    def test_any_other_failure_raises(self):
        for error in (http_error(403), http_error(500), OSError("unreachable"), ValueError("not JSON")):
            with self.subTest(error=error):
                with self.assertRaises(type(error)):
                    self.listing(error)


class NullIsNotMissing:
    """A null answer is not a 404: it fails, as it did before, so nothing acts
    on it as an empty list (#619 review)."""

    def test_a_null_answer_is_not_an_empty_list(self):
        with self.assertRaises(AttributeError):
            self.listing(None)


class FirewallTests(NullIsNotMissing, Tolerant, unittest.TestCase):
    MODULE = FIREWALL

    def test_a_404_is_empty_only_when_optional(self):
        self.assertEqual(self.listing(http_error(404), optional=True), [])
        with self.assertRaises(urllib.error.HTTPError):
            self.listing(http_error(404))

    def test_optional_reads_raise_any_other_failure(self):
        for code in (403, 500):
            with self.subTest(code=code):
                with self.assertRaises(urllib.error.HTTPError):
                    self.listing(http_error(code), optional=True)

    def test_a_null_answer_on_an_optional_read_is_not_an_empty_list(self):
        with self.assertRaises(AttributeError):
            self.listing(None, optional=True)

    def test_optional_reads_keep_rows_and_take_no_items_as_empty(self):
        self.assertEqual(self.listing({"items": [ROW]}, optional=True), [ROW])
        self.assertEqual(self.listing({}, optional=True), [])


class LhrebuildTests(NullIsNotMissing, Tolerant, unittest.TestCase):
    MODULE = LHREBUILD

    def test_a_404_is_empty(self):
        self.assertEqual(self.listing(http_error(404)), [])


class ContainerRebalanceTests(NullIsNotMissing, Tolerant, unittest.TestCase):
    MODULE = CREBALANCE

    def test_a_404_is_empty(self):
        self.assertEqual(self.listing(http_error(404)), [])


class UplinksTests(Tolerant, unittest.TestCase):
    MODULE = UPLINKS

    def test_a_404_is_empty(self):
        self.assertEqual(self.listing(http_error(404)), [])

    def test_an_empty_answer_or_null_items_is_empty(self):
        for answer in (None, {"items": None}, {"items": []}):
            with self.subTest(answer=answer):
                self.assertEqual(self.listing(answer), [])


if __name__ == "__main__":
    unittest.main()
