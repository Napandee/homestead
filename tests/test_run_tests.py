import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import run_tests as RUN


class ShardTests(unittest.TestCase):
    def test_every_test_file_runs_in_exactly_one_share(self):
        files = RUN.test_files()
        for count in (1, 3, 4):
            shares = [RUN.shard(files, k, count) for k in range(1, count + 1)]
            flat = [path for share in shares for path in share]
            self.assertEqual(sorted(files), sorted(flat), count)

    def test_shares_are_the_same_on_every_run(self):
        files = RUN.test_files()
        self.assertEqual(RUN.shard(files, 2, 4), RUN.shard(list(reversed(files)), 2, 4))


class ChangedTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = pathlib.Path(folder.name)
        self.files = []
        for name, text in {"test_widgets.py": "import homestead_widgets as W\n",
                           "test_gadgets.py": "import homestead_gadgets_extra\n",
                           "test_scan.py": "for p in ROOT.rglob('*.py'): pass\n",
                           "test_page.py": "read('web/js/views-vms.js')\n"}.items():
            (root / name).write_text(text)
            self.files.append(root / name)

        (root / "test_routes.py").write_text("server.GADGET_ROUTES.handle()\n")
        self.files.append(root / "test_routes.py")

    def names(self, changed):
        picked = RUN.affected(self.files, set(changed), {"homestead_gadgets": {"GADGET_ROUTES"}})
        return None if picked is None else sorted(p.name for p in picked)

    def test_a_module_change_runs_its_tests_and_the_tree_scans_only(self):
        self.assertEqual(["test_scan.py", "test_widgets.py"], self.names(["server/homestead_widgets.py"]))

    def test_a_module_reached_through_its_server_alias_runs_those_tests(self):
        self.assertEqual(["test_routes.py", "test_scan.py"], self.names(["server/homestead_gadgets.py"]))

    def test_a_web_file_change_runs_the_tests_that_read_it(self):
        self.assertEqual(["test_page.py", "test_scan.py"], self.names(["web/js/views-vms.js"]))

    def test_a_changed_test_file_runs_itself(self):
        self.assertEqual(["test_gadgets.py", "test_scan.py"], self.names(["tests/test_gadgets.py"]))

    def test_shared_files_and_test_helpers_run_everything(self):
        for changed in (["server/server.py"], ["tests/source_scan.py"], [".gitattributes"]):
            self.assertIsNone(self.names(changed), changed)


if __name__ == "__main__":
    unittest.main()
