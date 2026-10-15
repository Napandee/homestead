#!/usr/bin/env python3
"""Track 1 helper: what a function needs before it leaves server.py, and
whether everything it needs reached its new module afterwards.

  move_check.py plan   DEST NAME...   before the move: prints the module header,
                                      server.py's import lines and the tests to re-point
  move_check.py verify DEST NAME...   after the move: exit 1, with every problem, when a
                                      function in DEST uses a name DEST neither defines,
                                      imports nor binds, or server.py/tests still look
                                      for a moved name on server

Run from the repository root (else: exit 2, "run from the repository root"). DEST is a
module name, e.g. homestead_overview_server (a module with no file: exit 2, "no module").
"""
import ast, builtins, pathlib, re, sys

ROOT = pathlib.Path.cwd()
SERVER = ROOT / "server" / "server.py"
BUILTINS = set(dir(builtins)) | {"__file__", "__name__"}


def top_names(tree):
    """{name: kind} for server.py's top level: func, const, hmod:<module>, mod:<module>."""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)) and node.col_offset == 0:
            for a in node.names:
                name = (a.asname or a.name).split(".")[0]
                if isinstance(node, ast.ImportFrom) and node.module.startswith("homestead"):
                    out.setdefault(name, "func")      # moved earlier, imported back: reach it through _late
                    continue
                module = a.name if isinstance(node, ast.Import) else f"{node.module}.{a.name}"
                out.setdefault(name, ("hmod:" if module.startswith("homestead") else "mod:") + module)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            out[node.name] = "func" if isinstance(node, ast.FunctionDef) else "class"
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                for x in ast.walk(target):
                    if isinstance(x, ast.Name):
                        out.setdefault(x.id, "const")
    return out


def free_names(fn):
    """Names a function reads that it does not bind itself, and the globals it declares."""
    bound, read, declared = set(), set(), set()
    for x in ast.walk(fn):
        if isinstance(x, ast.arg):
            bound.add(x.arg)
        elif isinstance(x, ast.Name):
            (read if isinstance(x.ctx, ast.Load) else bound).add(x.id)
        elif isinstance(x, (ast.FunctionDef, ast.ClassDef)) and x is not fn:
            bound.add(x.name)
        elif isinstance(x, ast.ExceptHandler) and x.name:
            bound.add(x.name)
        elif isinstance(x, (ast.Import, ast.ImportFrom)):
            bound.update((a.asname or a.name).split(".")[0] for a in x.names)
        elif isinstance(x, ast.Global):
            declared.update(x.names)
    return ((read - bound) | declared) - BUILTINS, declared


def functions(tree):
    return {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def module_level(tree):
    """Every name a module binds at its top level, and its ROUTE_NEEDS/VALUES."""
    names, lists = set(), {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                for x in ast.walk(target):
                    if isinstance(x, ast.Name):
                        names.add(x.id)
                        if x.id in ("ROUTE_NEEDS", "VALUES") and isinstance(node.value, ast.Tuple):
                            lists[x.id] = {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
    return names, lists


def quoted(names):
    """A tuple literal written the way NEEDS tuples are: double quotes."""
    return "(" + ", ".join(f'"{name}"' for name in names) + ("," if len(names) == 1 else "") + ")"


def other_needs():
    """{name: [module.NEEDS, ...]} for every NEEDS/ROUTE_NEEDS tuple in server/homestead_*.py."""
    out = {}
    for path in sorted((ROOT / "server").glob("homestead_*.py")):
        for match in re.finditer(r"^(\w*NEEDS)\s*=\s*\((.*?)\)", path.read_text(), re.S | re.M):
            for name in re.findall(r'[\'"](\w+)[\'"]', match.group(2)):
                out.setdefault(name, []).append(f"{path.stem}.{match.group(1)}")
    return out


def server_references(tree, text, names, skip):
    """Lines in server.py, outside the functions in skip, that name one of names."""
    spans = [range(f.lineno, f.end_lineno + 1) for f in functions(tree).values() if f.name in skip]
    inside = lambda line: any(line in span for span in spans)
    hits = {}
    for x in ast.walk(tree):
        if isinstance(x, ast.Name) and x.id in names and not inside(x.lineno):
            hits.setdefault(x.id, set()).add(x.lineno)
    for number, line in enumerate(text.splitlines(), 1):
        for name in re.findall(r'["\'](\w+)["\']', line):
            if name in names and not inside(number):
                hits.setdefault(name, set()).add(number)
    return hits


def tests_naming(names):
    out = {}
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        text = path.read_text()
        for name in names:
            if re.search(rf"\bserver\.{re.escape(name)}\b|patch\.object\(server, \"{re.escape(name)}\"", text):
                out.setdefault(name, []).append(path.name)
    return out


def plan(dest, names):
    text = SERVER.read_text()
    tree = ast.parse(text)
    kinds, defs = top_names(tree), functions(tree)
    missing = [n for n in names if n not in defs]
    if missing:
        sys.exit("not a top-level function of server.py: " + ", ".join(missing))
    needs, values, imports, stdlib, written = set(), set(), {}, set(), {}
    for name in names:
        free, declared = free_names(defs[name])
        if declared:
            written[name] = sorted(declared)
        for used in free:
            kind = kinds.get(used, "?")
            if used in names:
                continue
            if kind in ("func", "class"):
                needs.add(used)
            elif kind == "const":
                values.add(used)
            elif kind.startswith("hmod:"):
                imports[used] = kind[5:]
            elif kind.startswith("mod:"):
                stdlib.add(kind[4:])
            else:
                print(f"!! {name} uses {used}, which server.py does not define")
    if written:
        print("!! these assign server.py globals and cannot move:", written)
    refs = server_references(tree, text, set(names), set(names))
    uses = other_needs()
    still = sorted(n for n in names if n in refs or n in uses)
    alias = dest.replace("homestead_", "").upper()
    print(f"# ---- {dest}.py header ----")
    for module in sorted(stdlib):
        if module.startswith("urllib"):
            print("import urllib.request, urllib.parse, urllib.error")
        elif "." in module:
            print(f"from {module.rsplit('.', 1)[0]} import {module.rsplit('.', 1)[1]}")
        else:
            print(f"import {module}")
    for name, module in sorted(imports.items(), key=lambda kv: kv[1]):
        print(f"import {module} as {name}")
    value_refs = server_references(tree, text, values, set(names))
    own = {v for v in values if len(value_refs.get(v, ())) == 1}
    print(f"\nROUTE_NEEDS = {quoted(sorted(needs))}")
    print(f"VALUES = {quoted(sorted(values - own))}")
    if own:
        print(f"# move these constants into {dest} (nothing else in server.py uses them): {', '.join(sorted(own))}")
    print(f"\n# ---- server.py ----\nimport {dest} as {alias}")
    if still:
        print(f"from {dest} import (")
        for i in range(0, len(still), 5):
            print("    " + ", ".join(still[i:i + 5]) + ",")
        print(")")
    print(f"{alias}.bind_routes(_late(*{alias}.ROUTE_NEEDS), **{{name: globals()[name] for name in {alias}.VALUES}})")
    print("\n# ---- server.py still names these (why each is imported back) ----")
    for name in still:
        print(f"{name}: lines {sorted(refs.get(name, []))} {uses.get(name, '')}")
    print("\n# ---- tests that call server.NAME: re-point to server.%s.NAME ----" % alias)
    for name, files in sorted(tests_naming(names).items()):
        print(f"{name}: {' '.join(files)}")
    tests_text = "".join(p.read_text() for p in (ROOT / "tests").glob("test_*.py"))
    patched = {m for pair in re.findall(r'patch\.object\(server, "(\w+)"|\bserver\.(\w+) = ', tests_text) for m in pair if m}
    hits = sorted(patched & (values | set(imports)))
    if hits:
        print("\n# ---- tests patch these on server; where such a test reaches moved code, patch the module too ----")
        print(" ".join(hits))


def verify(dest, names):
    problems = []
    tree = ast.parse((ROOT / "server" / f"{dest}.py").read_text())
    defs = functions(tree)
    bound, lists = module_level(tree)
    needs, values = lists.get("ROUTE_NEEDS", set()), lists.get("VALUES", set())
    for name in names:
        if name not in defs:
            problems.append(f"{name} is not defined in {dest}")
    # Every function in the module, the route handlers too, and the lambdas in ROUTES.
    checked = [(fn.name, fn) for fn in defs.values()]
    checked += [(f"lambda at line {x.lineno}", x) for node in tree.body if not isinstance(node, ast.FunctionDef)
                for x in ast.walk(node) if isinstance(x, ast.Lambda)]
    for label, fn in checked:
        free, declared = free_names(fn)
        for used in sorted(free):
            if used not in bound and used not in needs and used not in values:
                problems.append(f"{label} uses {used}: define, import or bind it in {dest}")
        if declared:
            problems.append(f"{label} declares global {sorted(declared)}: it writes state server.py reads")
    for name in sorted(needs & set(defs)):
        problems.append(f"{name} is defined in {dest} and also in its ROUTE_NEEDS: bind_routes would replace it")
    server_text = SERVER.read_text()
    server_tree = ast.parse(server_text)
    for name in names:
        if name in functions(server_tree):
            problems.append(f"server.py still defines {name}")
    imported = set()
    for node in server_tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == dest:
            imported.update(a.name for a in node.names)
    refs = server_references(server_tree, server_text, set(names), set())
    uses = other_needs()
    for name in names:
        if (name in refs or name in uses) and name not in imported:
            problems.append(f"server.py still names {name} (lines {sorted(refs.get(name, []))} {uses.get(name, '')}): "
                            f"add it to `from {dest} import (...)`")
        if name in imported and name not in refs and name not in uses:
            problems.append(f"server.py imports {name} from {dest} but never names it: take it out of the import")
    for name, files in sorted(tests_naming(names).items()):
        problems.append(f"{', '.join(files)} still call server.{name}: point them at the module")
    for line in problems:
        print(line)
    print(f"{len(checked)} functions checked in {dest}: " + ("OK" if not problems else f"{len(problems)} problems"))
    return 1 if problems else 0


if __name__ == "__main__":
    if len(sys.argv) < 4 or sys.argv[1] not in ("plan", "verify"):
        sys.exit(__doc__)
    if not SERVER.exists():
        print(f"{SERVER} not found: run from the repository root", file=sys.stderr)
        sys.exit(2)
    if sys.argv[1] == "verify" and not (ROOT / "server" / f"{sys.argv[2]}.py").exists():
        print(f"no module server/{sys.argv[2]}.py", file=sys.stderr)
        sys.exit(2)
    sys.exit(plan(sys.argv[2], sys.argv[3:]) if sys.argv[1] == "plan" else verify(sys.argv[2], sys.argv[3:]))
