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
    "homestead_longhorn": ("homestead_longhorn_jobs", "homestead_longhorn_volumes", "homestead_longhorn_target",
                           "homestead_longhorn_backups", "homestead_longhorn_restore"),
    "homestead_networking": ("homestead_network_state", "homestead_network_inventory",
                             "homestead_network_services", "homestead_vip_registry", "homestead_network_vms"),
    "homestead_deploy": ("homestead_deploy_appstore", "homestead_deploy_manifests"),
    "homestead_lifecycle": ("homestead_workload_edit", "homestead_node_actions"),
}

# What a module handed to bind() reads through it, by holder: the files and how
# they name the module they were given.
HOLDERS = {
    # server.py hands homestead_longhorn to the move source and the move engine,
    # which keeps it on homestead_move_state; their tests hand in a fake.
    "homestead_longhorn": (("homestead_move_source.py", r"(?<![\w.])LH\.(\w+)"),
                           ("homestead_move_state.py", r"(?<![\w.])LH\.(\w+)"),
                           ("homestead_move_*.py", r"\bSTATE\.LH\.(\w+)")),
    # ... and homestead_networking to the move engine and to the self address.
    "homestead_networking": (("homestead_move_state.py", r"(?<![\w.])NETWORK\.(\w+)"),
                             ("homestead_move_*.py", r"\bSTATE\.NETWORK\.(\w+)"),
                             ("homestead_self_address.py", r"(?<![\w.])network\.(\w+)")),
}

# The names a split module still offers of its parts, for its HOLDERS.
INTERFACE = {
    "homestead_longhorn": ("backup_target", "backup_target_state", "backups", "ensure_move_backup",
                           "ensure_restore_support", "replace_backup_target_state", "restore_backup",
                           "restore_problem", "restore_support", "transfer_target_state",
                           "validate_restore_class"),
    "homestead_networking": ("create_service", "inventory", "service_plan", "servicelb_present",
                             "set_default_vip"),
}


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
