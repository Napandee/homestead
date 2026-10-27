"""The #623 splits no longer re-export what they moved (#623).

Each module #623 split kept every moved name for one release, so other
modules and tests kept working while they moved over. That release is over:
a name is read, and replaced in tests, on the module that defines it. A read
through the old module would miss a test's replacement in the part, and a
replacement on the old module would miss the part's own code.

What a split module still offers of its parts is only what a module it is
handed to in bind() reads through it: there it is the object the module was
given (a test hands in a fake with the same names), not a module read by name.
That list is pinned both ways below.
"""
import ast
import importlib
import re
import sys
import unittest

import source_scan as SS

sys.path.insert(0, str(SS.SERVER))

# Each split module and the parts it was split into.
SPLITS = {
    "homestead_move_engine": ("homestead_move_state", "homestead_move_target", "homestead_move_plan",
                              "homestead_move_phases", "homestead_move_actions"),
}

# What a module handed to bind() reads through it, by holder: the file and how
# it names the module it was given.
HOLDERS = {}

# The names a split module still offers of its parts, for its HOLDERS.
INTERFACE = {}


def defined(part):
    """The functions and classes a part defines."""
    module = importlib.import_module(part)
    return {name for name, value in vars(module).items() if not name.startswith("__")
            and getattr(value, "__module__", None) == part}


def imported_from_parts(old):
    """Names the split module imports from one of its parts with `from part import`."""
    return {alias.asname or alias.name for node in SS.tree(SS.SERVER / f"{old}.py").body
            if isinstance(node, ast.ImportFrom) and node.module in SPLITS[old] for alias in node.names}


def holder_reads(old):
    found = set()
    for path, pattern in HOLDERS.get(old, ()):
        for file in sorted(SS.SERVER.glob(path)):
            found |= set(re.findall(pattern, SS.text(file)))
    return found


class SplitReexportTests(unittest.TestCase):
    def test_a_split_module_imports_from_its_parts_only_what_its_holders_read(self):
        for old in SPLITS:
            with self.subTest(old):
                self.assertEqual(sorted(INTERFACE.get(old, ())), sorted(imported_from_parts(old)))

    def test_no_moved_function_or_class_is_left_on_the_split_module(self):
        for old, parts in SPLITS.items():
            module = importlib.import_module(old)
            for part in parts:
                left = sorted(name for name in defined(part) - set(INTERFACE.get(old, ()))
                              if getattr(module, name, None) is getattr(importlib.import_module(part), name))
                self.assertEqual([], left, f"{old} still offers these of {part}")

    def test_the_interface_is_exactly_what_the_holders_read(self):
        for old, parts in SPLITS.items():
            moved = set().union(*(vars(importlib.import_module(part)) for part in parts))
            with self.subTest(old):
                self.assertEqual(sorted(INTERFACE.get(old, ())), sorted(holder_reads(old) & moved))


if __name__ == "__main__":
    unittest.main()
