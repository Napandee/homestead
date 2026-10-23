"""Reading Homestead's own source, for the guard tests (#732).

A guard test reads the source and fails when a structural rule is broken.
This is the one place those tests read files and pick them apart, so each
guard does not write its own little parser with its own idea of comments,
strings and what a function is. Standard library only.

- text(path): a file's text, read once per run.
- server_modules(): every server/homestead_*.py, in a fixed order.
- tree(source), functions(source), calls(source, *names): Python, found
  with ast, so a commented-out def or call is never counted.
- js_code(source), js_calls(source, name): JavaScript, read as text with
  comments, strings and regular expressions blanked out, so a call written
  in one of those is never counted.

`source` names a file, as for text(): a Path, or a str path relative to the
repository root. Source text itself goes in as code("..."), and tree() and
calls() also take a tree already parsed. A str holding source text is
refused rather than read as a path: either way round, a mix-up would parse
into something that finds nothing, and the guard would pass for ever.

Files are read and parsed once per run: don't read a file through here that
a test writes.
"""
import ast
import functools
import re
from collections import namedtuple
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
SERVER_PY = SERVER / "server.py"

Function = namedtuple("Function", "name node text")
JsCall = namedtuple("JsCall", "line args")


class Code(str):
    """Source text, as opposed to the path of a file."""


def code(source_text):
    """Mark source text, so the helpers parse it instead of reading a file."""
    return Code(source_text)


def _path(path):
    if isinstance(path, str) and not isinstance(path, Code) and "\n" in path:
        raise TypeError("source text given where a file is expected: pass it as source_scan.code(...)")
    path = Path(path)
    return (path if path.is_absolute() else ROOT / path).resolve()


@functools.lru_cache(maxsize=None)
def _read(path):
    return path.read_text(encoding="utf-8")


def text(path):
    """The text of a file, read once per run. A relative path is taken from
    the repository root."""
    return _read(_path(path))


def server_modules():
    """Every server/homestead_*.py, sorted by name."""
    return sorted(SERVER.glob("homestead_*.py"))


@functools.lru_cache(maxsize=None)
def _parsed(path):
    return ast.parse(_read(path), filename=str(path))


def tree(source):
    """The ast of a file (parsed once per run), of code("..."), or the tree
    itself when it is one already."""
    if isinstance(source, ast.AST):
        return source
    if isinstance(source, Code):
        return ast.parse(source)
    return _parsed(_path(source))


def _source_text(source):
    if isinstance(source, ast.AST):
        raise TypeError("functions() needs a file or code(...), to give each function's text")
    return source if isinstance(source, Code) else text(source)


def functions(source):
    """Every def and async def, nested ones too, by qualified name
    ("H.do_GET", "outer.inner"), in source order. Each comes with its
    node and its text: from its first decorator (or its def line) through
    its last statement. Class attributes, and comments between or after
    functions, belong to none. Two defs with one name: the last one wins."""
    lines = _source_text(source).splitlines()
    found = {}

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + child.name
                if not isinstance(child, ast.ClassDef):
                    first = min([child.lineno, *(d.lineno for d in child.decorator_list)])
                    found[name] = Function(name, child, "\n".join(lines[first - 1:child.end_lineno]))
                visit(child, name + ".")
            else:
                visit(child, prefix)

    visit(tree(source), "")
    return found


def dotted(node):
    """"OPS.start" for OPS.start, "go" for go; None for anything else
    (a call on a call, a subscript)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return None if base is None else f"{base}.{node.attr}"
    return None


def calls(source, *names):
    """Every call whose callee is written as one of names ("OPS.start",
    "go"), in source order. A call in a comment or a string is not a call."""
    wanted = set(names)
    found = [node for node in ast.walk(tree(source))
             if isinstance(node, ast.Call) and dotted(node.func) in wanted]
    return sorted(found, key=lambda node: (node.lineno, node.col_offset))


_REGEX_AFTER = re.compile(r"(^|[=(,:;!&|?{}\[+\-*%<>~^]|\breturn|\btypeof|\bcase)$")


def _js_text(source):
    if isinstance(source, ast.AST):
        raise TypeError("JavaScript has no Python tree: pass a file or code(...)")
    return source if isinstance(source, Code) else text(source)


def js_code(source):
    """A JavaScript file's code with its comments, the text of its strings and
    templates, and its regular expressions blanked to spaces, where they stood:
    the same length and the same lines, so an offset in one is an offset in
    the other. Quotes, backticks and the code inside ${...} are kept. (The
    scanner of tests/helpers/js-strings.js.)"""
    if isinstance(source, Code):
        return _blank(source)
    return _blank_file(_path(source))


@functools.lru_cache(maxsize=None)
def _blank_file(path):
    return _blank(_read(path))


def _blank(src):
    out = list(src)
    n = len(src)
    i = 0

    def blank(start, end):
        for k in range(start, min(end, n)):
            if out[k] != "\n":
                out[k] = " "

    def regex_can(at):
        start = at
        while start > 0 and src[start - 1].isspace():
            start -= 1
        return bool(_REGEX_AFTER.search(src[max(0, start - 8):start]))

    def quoted(quote):
        nonlocal i
        start = i = i + 1
        while i < n and src[i] != quote and src[i] != "\n":
            i += 2 if src[i] == "\\" else 1
        blank(start, i)
        i += 1

    def template():
        nonlocal i
        i += 1
        start = i
        while i < n and src[i] != "`":
            if src[i] == "\\":
                i += 2
                continue
            if src.startswith("${", i):
                blank(start, i)
                i += 2
                code(True)
                start = i
                continue
            i += 1
        blank(start, i)
        i += 1

    def code(inner):
        nonlocal i
        depth = 0
        while i < n:
            c = src[i]
            if src.startswith("//", i):
                end = src.find("\n", i)
                end = n if end < 0 else end
                blank(i, end)
                i = end
            elif src.startswith("/*", i):
                end = src.find("*/", i + 2)
                end = n if end < 0 else end + 2
                blank(i, end)
                i = end
            elif c == "/" and regex_can(i):
                i += 1
                start, in_class = i, False
                while i < n and (in_class or src[i] != "/") and src[i] != "\n":
                    if src[i] == "\\":
                        i += 1
                    elif src[i] == "[":
                        in_class = True
                    elif src[i] == "]":
                        in_class = False
                    i += 1
                blank(start, i)
                i += 1
            elif c in "\"'":
                quoted(c)
            elif c == "`":
                template()
            else:
                if c == "{":
                    depth += 1
                elif c == "}":
                    if inner and depth == 0:
                        i += 1
                        return
                    depth -= 1
                i += 1

    code(False)
    return "".join(out)


_DECLARED = re.compile(r"\bfunction\s*\*?\s*$")


def js_calls(source, name):
    """Every call of the bare function name ("api"), in source order, each
    with its line and its arguments as written. x.name(...) and myname(...)
    are other functions; a call in a comment, a string or a regular
    expression is not a call, and neither is a declaration:
    `function name(a, b) {` or the method shorthand `name(a) {`."""
    src = _js_text(source)
    blanked = js_code(source)
    found = []
    for match in re.finditer(r"(?<![\w$.])" + re.escape(name) + r"\s*\(", blanked):
        line = blanked.count("\n", 0, match.start()) + 1
        depth, cut, args = 0, match.end(), []
        for at in range(match.end(), len(blanked)):
            c = blanked[at]
            if c in "([{":
                depth += 1
            elif c in ")]}" and depth:
                depth -= 1
            elif c == "," and not depth:
                args.append(src[cut:at].strip())
                cut = at + 1
            elif c == ")":
                last = src[cut:at].strip()
                if last:
                    args.append(last)
                break
        else:
            raise ValueError(f"{name}( on line {line} is never closed")
        if _DECLARED.search(blanked[max(0, match.start() - 20):match.start()]) or re.match(r"\s*\{", blanked[at + 1:at + 40]):
            continue
        found.append(JsCall(line, args))
    return found
