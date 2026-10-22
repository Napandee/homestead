"""homestead_move_engine split by responsibility (#623): the bound clients,
records and shared helpers live in homestead_move_state, the backup target in
homestead_move_target, reviewing and starting a move in homestead_move_plan, the
phases and the tick in homestead_move_phases, and retry, abandon, dismiss and
finish in homestead_move_actions. homestead_move_engine keeps bind(),
after_finish, the routes and the Activity tray registration, and re-exports
every moved name except the bound clients."""
import ast
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

import homestead_move_actions as ACT
import homestead_move_engine as ENGINE
import homestead_move_phases as PHASE
import homestead_move_plan as PLAN
import homestead_move_state as STATE
import homestead_move_target as TARGET

PARTS = (STATE, TARGET, PLAN, PHASE, ACT)
NAMES = ("homestead_move_state", "homestead_move_target", "homestead_move_plan",
         "homestead_move_phases", "homestead_move_actions", "homestead_move_engine")
# What server.py binds: only homestead_move_state holds it, so that a test or a
# module setting one of them on homestead_move_engine is not silently ignored.
BOUND = ("kget", "ksend", "LH", "CLIENT", "NETWORK", "OPS", "DATA_DIR", "NS")
# What homestead_move_engine defines itself.
OWN = ("guard_managed_smb", "bind", "after_finish", "_plan_or_start", "ROUTES", "register")


# Objects whose class lives elsewhere, so __module__ does not say they were moved.
# A second copy of one of these locks would split the locking in two.
LOCKS = {STATE: ("_lock", "_step_locks", "_step_locks_guard"), TARGET: ("_target_lock",)}


def moved(part):
    return [name for name in vars(part) if not name.startswith("__")
            and getattr(vars(part)[name], "__module__", part.__name__) == part.__name__
            and name not in BOUND and not isinstance(vars(part)[name], type(sys))] + list(LOCKS.get(part, ()))


class MoveEngineModuleTests(unittest.TestCase):
    def test_every_moved_name_is_the_same_object_on_homestead_move_engine(self):
        for part in PARTS:
            names = moved(part)
            self.assertTrue(names, part.__name__)
            for name in names:
                self.assertIs(getattr(ENGINE, name), getattr(part, name), f"{part.__name__}.{name}")

    def test_the_bound_clients_live_only_on_homestead_move_state(self):
        for name in BOUND:
            self.assertTrue(hasattr(STATE, name), name)
            self.assertFalse(hasattr(ENGINE, name), f"homestead_move_engine.{name} would be a stale copy")

    def test_each_module_can_be_imported_first(self):
        for name in NAMES:
            done = subprocess.run([sys.executable, "-c", f"import {name}, homestead_move_engine as E; "
                                   "E.tick_all; E.carry_icon; E.abandon; E._find; E.ROUTES"],
                                  cwd=SERVER, capture_output=True, text=True)
            self.assertEqual(0, done.returncode, f"importing {name} first: {done.stderr}")

    def test_the_routes_stay_on_homestead_move_engine(self):
        self.assertEqual({("GET", "/api/move/moves"), ("POST", "/api/move/moves/dismiss"),
                          ("POST", "/api/move/plan"), ("POST", "/api/move/start"),
                          ("POST", "/api/move/moves/retry"), ("POST", "/api/move/moves/abandon"),
                          ("POST", "/api/move/moves/finish")}, set(ENGINE.ROUTES))
        for part in PARTS:
            self.assertFalse(hasattr(part, "ROUTES"), part.__name__)

    def test_the_unused_run_loop_is_gone(self):
        # The leader's loop in homestead_deploy_server calls tick_all; nothing called run().
        for module in PARTS + (ENGINE,):
            self.assertFalse(hasattr(module, "run"), module.__name__)

    def test_bind_reaches_the_moved_code(self):
        saved = {name: getattr(STATE, name) for name in BOUND}
        self.addCleanup(lambda: [setattr(STATE, name, value) for name, value in saved.items()])
        saved_guard = ENGINE.guard_managed_smb
        self.addCleanup(setattr, ENGINE, "guard_managed_smb", saved_guard)
        asked, sent = [], []
        kget = lambda path: asked.append(path) or {}
        ksend = lambda *args, **_: sent.append(args)
        with tempfile.TemporaryDirectory() as directory, mock.patch("homestead_names.bind"):
            ENGINE.bind(kget, ksend, "longhorn", "client", "network", "operations", directory, "apps")
            self.assertEqual((kget, ksend, "longhorn", "client", "network", "operations", directory, "apps"),
                             tuple(getattr(STATE, name) for name in BOUND))
            PHASE._ensure_namespace("apps")
            self.assertEqual(["/api/v1/namespaces/apps"], asked)
            self.assertEqual([], STATE.moves())
            self.assertEqual({}, TARGET._target_journal())

    def test_a_guard_given_to_bind_runs_before_a_container_move_starts(self):
        saved = {name: getattr(STATE, name) for name in BOUND}
        self.addCleanup(lambda: [setattr(STATE, name, value) for name, value in saved.items()])
        self.addCleanup(setattr, ENGINE, "guard_managed_smb", ENGINE.guard_managed_smb)
        guarded = []
        with mock.patch("homestead_names.bind"):
            ENGINE.bind(None, None, None, None, None, None, "/data", "apps",
                        guard=lambda ns, name: guarded.append((ns, name)))
        request = SimpleNamespace(body={"cluster": "other", "name": "web"})
        with mock.patch.object(PLAN, "start", return_value={"id": "m"}) as start:
            ENGINE._plan_or_start(request, True)
        self.assertEqual([("apps", "web")], guarded)
        self.assertEqual(("other", "container", "web", "apps"), start.call_args.args[:4])

    def test_after_finish_set_on_homestead_move_engine_runs_when_a_move_finishes(self):
        # server.py sets after_finish on homestead_move_engine.
        finished = []
        with mock.patch.object(ENGINE, "after_finish", lambda: finished.append(True)), \
                mock.patch.object(STATE, "_store"), mock.patch.object(PHASE, "_cleanup_resources"), \
                mock.patch.object(TARGET, "_cleanup_target"):
            PHASE._finish({"id": "0123456789ab"}, "failed", "stopped")
        self.assertEqual([True], finished)

    def test_no_test_replaces_a_moved_name_on_homestead_move_engine(self):
        # A replacement there would not reach the moved code, so it must go to the
        # module that defines the name. Read: assignments, setattr, mock.patch(...),
        # patch.object and patch.multiple, on the engine as a test imported it, as it
        # took it from another test module (from test_move_flow import engine,
        # fixtures.engine), or as server.py names it (server.MOVE_ENGINE). Not read:
        # a helper that is handed the module and replaces a name on it itself.
        found = []
        trees = {path: ast.parse(path.read_text()) for path in sorted((ROOT / "tests").glob("test_*.py"))}
        imported = {path.stem: {a.asname or a.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                                for a in node.names if a.name == "homestead_move_engine"}
                    for path, tree in trees.items()}
        anywhere = set().union(*imported.values()) | {"MOVE_ENGINE"}
        for path, tree in trees.items():
            aliases = imported[path.stem] | {a.asname or a.name for node in ast.walk(tree)
                                             if isinstance(node, ast.ImportFrom) and node.module in imported
                                             for a in node.names if a.name in imported[node.module]}

            def engine(node):
                return (isinstance(node, ast.Name) and node.id in aliases) or \
                    (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.attr in anywhere)

            for node in ast.walk(tree):
                name = None
                if isinstance(node, (ast.Assign, ast.AugAssign)):
                    for target in getattr(node, "targets", [getattr(node, "target", None)]):
                        if isinstance(target, ast.Attribute) and engine(target.value):
                            name = target.attr
                elif isinstance(node, ast.Call) and isinstance(node.func, (ast.Attribute, ast.Name)):
                    called = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
                    first = node.args[0] if node.args else None
                    keywords = {k.arg: k.value for k in node.keywords}
                    second = node.args[1] if len(node.args) > 1 else keywords.get("attribute")
                    if called in ("object", "setattr") and first is not None and engine(first) \
                            and isinstance(second, ast.Constant):
                        name = second.value
                    elif called == "multiple" and first is not None and engine(first):
                        name = ",".join(k.arg for k in node.keywords)
                    elif called == "patch" and isinstance(first, ast.Constant) \
                            and str(first.value).startswith("homestead_move_engine."):
                        name = first.value.split(".")[1]
                if name and name not in OWN:
                    found.append(f"{path.name}:{node.lineno} {name}")
        self.assertEqual([], found)

if __name__ == "__main__":
    unittest.main()
