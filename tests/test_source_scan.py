"""The shared reader the guard tests use (tests/source_scan.py, #732)."""
import ast
import unittest
from pathlib import Path

import source_scan as SS

PY = SS.code('''\
import os

def go(kind):
    OPS.start("copy", "x")
    # OPS.start("commented", "x")
    text = 'OPS.start("in-a-string")'
    ops.start(kind)
    return x.OPS.start("other")


class H:
    def do_GET(self):
        def inner():
            pass
        return go("a")

    async def _helper(self):
        go("b")
''')

CLASS = SS.code('''\
class H:
    PUBLIC = ("/api/attribute",)

    @wrap("/api/decorator")
    def one(self):
        return 1
        # a trailing comment

    # between one and two
    def two(self):
        pass
''')


class Files(unittest.TestCase):
    def test_text_reads_a_file_once_per_run(self):
        first = SS.text("server/server.py")
        self.assertIs(first, SS.text(SS.SERVER_PY))
        self.assertEqual(SS.SERVER_PY.read_text(encoding="utf-8"), first)

    def test_server_modules_are_every_homestead_module_in_name_order(self):
        found = SS.server_modules()
        self.assertEqual(sorted((SS.ROOT / "server").glob("homestead_*.py")), found)
        self.assertEqual([path.name for path in found], sorted(path.name for path in found))
        self.assertNotIn(SS.SERVER_PY, found)
        self.assertGreater(len(found), 100)

    def test_a_file_is_parsed_once_per_run(self):
        self.assertIs(SS.tree(SS.SERVER_PY), SS.tree(Path("server/server.py")))
        self.assertIs(SS.tree(SS.SERVER_PY), SS.tree("server/server.py"))
        parsed = ast.parse("x = 1")
        self.assertIs(parsed, SS.tree(parsed))


class PathsAndCode(unittest.TestCase):
    """A str names a file, as for text(); source text is passed as code(...).
    A path read as code would parse (server/x.py is a division) and find
    nothing, so a guard written that way would pass for ever."""

    def test_a_str_path_is_read_as_the_file(self):
        self.assertTrue(SS.calls("server/server.py", "threading.Thread"))
        self.assertIn("H.do_GET", SS.functions("server/server.py"))

    def test_a_missing_file_is_an_error_not_an_empty_answer(self):
        with self.assertRaises(FileNotFoundError):
            SS.calls("server/homestead_no_such_module.py", "OPS.start")

    def test_source_text_not_marked_as_code_is_refused(self):
        with self.assertRaisesRegex(TypeError, r"source_scan\.code"):
            SS.calls("def go():\n    f()\n", "f")
        self.assertEqual([2], [node.lineno for node in SS.calls(SS.code("def go():\n    f()\n"), "f")])


class Functions(unittest.TestCase):
    def test_every_function_by_qualified_name_in_source_order(self):
        self.assertEqual(["go", "H.do_GET", "H.do_GET.inner", "H._helper"], list(SS.functions(PY)))

    def test_a_function_s_text_runs_from_its_def_to_its_last_line(self):
        found = SS.functions(PY)["H.do_GET"]
        self.assertEqual("    def do_GET(self):\n        def inner():\n            pass\n        return go(\"a\")",
                         found.text)
        self.assertIsInstance(found.node, ast.FunctionDef)
        self.assertIsInstance(SS.functions(PY)["H._helper"].node, ast.AsyncFunctionDef)

    def test_a_decorated_function_s_text_starts_at_its_decorator(self):
        found = SS.functions(SS.code("x = 1\n\n@wrap(\"/api/x\")\ndef go():\n    pass\n"))["go"]
        self.assertEqual("@wrap(\"/api/x\")\ndef go():\n    pass", found.text)

    def test_a_decorated_method_s_text_starts_at_its_decorator(self):
        self.assertTrue(SS.functions(CLASS)["H.one"].text.startswith('    @wrap("/api/decorator")\n    def one'))

    def test_class_attributes_and_comments_around_functions_belong_to_none(self):
        texts = "\n".join(found.text for found in SS.functions(CLASS).values())
        self.assertNotIn("/api/attribute", texts)
        self.assertNotIn("a trailing comment", texts)
        self.assertNotIn("between one and two", texts)

    def test_a_commented_out_def_is_not_a_function(self):
        self.assertEqual(["live"], list(SS.functions(SS.code("# def gone():\n#     pass\ndef live():\n    pass\n"))))

    def test_a_tree_alone_has_no_text_to_give(self):
        with self.assertRaises(TypeError):
            SS.functions(ast.parse(PY))


class Calls(unittest.TestCase):
    def test_calls_are_matched_by_how_the_callee_is_written(self):
        found = SS.calls(PY, "OPS.start", "ops.start")
        self.assertEqual([4, 7], [node.lineno for node in found])
        self.assertEqual(["copy"], [node.args[0].value for node in found[:1]])

    def test_a_call_in_a_comment_or_a_string_is_not_a_call(self):
        self.assertEqual([4], [node.lineno for node in SS.calls(PY, "OPS.start")])

    def test_a_bare_name_matches_only_a_bare_call(self):
        self.assertEqual([15, 18], [node.lineno for node in SS.calls(PY, "go")])
        self.assertEqual([8], [node.lineno for node in SS.calls(PY, "x.OPS.start")])

    def test_dotted_names_only_plain_names_and_attributes(self):
        self.assertEqual("a.b.c", SS.dotted(ast.parse("a.b.c", mode="eval").body))
        self.assertIsNone(SS.dotted(ast.parse("a().b", mode="eval").body))

    def test_a_file_is_read_by_path(self):
        self.assertTrue(SS.calls(SS.SERVER_PY, "threading.Thread"))


if __name__ == "__main__":
    unittest.main()
