"use strict";

/* A test that needs one piece of a source file cuts it with
   tests/helpers/between.js, which throws when a marker is missing (#623).
   Cut by hand, `src.slice(src.indexOf("marker"), ...)` takes the wrong text
   when the marker moves or is reworded, and the test can still pass.

   The check reads each call to slice, substring or substr, across lines, and
   reports it when its arguments call indexOf, lastIndexOf or search, or use a
   name the file set from one of those. It is cheap, not complete: a cut made
   with match() or split() is not looked for, and cutting rendered HTML at
   run time is reported too; between() does that job as well. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const INDEX = /\b(?:indexOf|lastIndexOf|search)\(/;
// A name given an index: `at = src.indexOf(`, `end = text.slice(from).search(`.
const NAMED = /([A-Za-z_$][\w$]*)\s*(?<![=!<>])=(?![=>])[^;,=\n]*?\.(?:indexOf|lastIndexOf|search)\(/g;

// The text between a call's brackets, from the "(" at `open`, past strings and escapes.
function args(text, open) {
  let depth = 0, quote = "";
  for (let i = open; i < text.length; i++) {
    const c = text[i];
    if (c === "\\") { i++; continue; }
    if (quote) { if (c === quote) quote = ""; continue; }
    if (c === '"' || c === "'" || c === "`") quote = c;
    else if (c === "(") depth++;
    else if (c === ")" && --depth === 0) return text.slice(open + 1, i);
  }
  return text.slice(open + 1);
}

// The line numbers of the hand cuts in a file's text.
function handCuts(text) {
  const names = [...text.matchAll(NAMED)].map(m => m[1]);
  const usesName = piece => names.some(name => new RegExp(`(^|[^\\w$.])${name.replace(/\$/g, "\\$")}(?![\\w$])`).test(piece));
  const lines = [];
  for (const m of text.matchAll(/\.(?:slice|substring|substr)\(/g)) {
    const piece = args(text, m.index + m[0].length - 1);
    if (INDEX.test(piece) || usesName(piece)) lines.push(text.slice(0, m.index).split("\n").length);
  }
  return [...new Set(lines)];
}

const ROOT = path.join(__dirname);
// between() itself, this file's examples, and js-strings.js, a scanner that
// walks a file by position rather than cutting it at a marker.
const SKIP = new Set(["helpers/between.js", "helpers/js-strings.js", "no-marker-slicing.test.js"]);
function files(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap(entry => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) return entry.name === "node_modules" ? [] : files(full);
    return /\.m?js$/.test(entry.name) ? [full] : [];
  });
}

// Each case is a piece of a test file and the lines handCuts() reports in it.
const CASES = [
  // Cut by hand: the index is taken in the call, or kept in a name of any kind first.
  ['vm.runInContext(core.slice(core.indexOf("const fmtUp"), core.indexOf("const upChip")), ctx);', [1]],
  ['const render = helm.slice(helm.search(/function renderHelm\\(/));', [1]],
  ['const block = src.slice(src.lastIndexOf("/* Tail"));', [1]],
  ['const block = src.substr(src.indexOf("/* Tail"), 400);', [1]],
  ['const block = src.substring(src.indexOf("a"), src.indexOf("b"));', [1]],
  // update-window on dev (#601): the bounds were named, the cut came two lines later.
  ['const writeAt = src.indexOf("window.settingsWrite"), writeEnd = src.indexOf("window.permissionsRecheck");\n' +
   'assert.ok(writeAt >= 0 && writeEnd > writeAt);\nconst write = src.slice(writeAt, writeEnd);', [3]],
  ['const at = core.indexOf("function foo(");\nconst body = core.slice(at, core.indexOf("\\n}\\n", at));', [2]],
  ['const i = src.indexOf("x");\nconst piece = src.slice(i);', [2]],
  ['const end = text.slice(from).search(/\\n\\};?\\n/);\nconst body = text.slice(0, end);', [2]],
  // A formatter's line breaks inside the call.
  ['const piece = src.slice(\n  src.indexOf("function foo("),\n  src.indexOf("function bar("),\n);', [1]],
  // Not a cut: an order check, a splice, an index read after a slice, between() itself.
  ['assert.ok(html.indexOf("Waiting for approval") < html.indexOf("Recent requests"));', []],
  ['const from = html.indexOf("A"), to = html.indexOf("B");\nassert.ok(from < to);', []],
  ['this.rows.splice(this.rows.indexOf(row), 1);', []],
  ['assert.equal(out.slice(-3).indexOf("x"), -1);', []],
  ['const block = between(ui, "/* Back to top (#292)", null);', []],
];

test("the check spots a hand cut and leaves other uses of indexOf alone", () => {
  for (const [text, lines] of CASES) assert.deepEqual(handCuts(text), lines, text);
});

test("no test cuts a source file at a marker by hand", () => {
  const found = [];
  for (const file of files(ROOT)) {
    const name = path.relative(ROOT, file).split(path.sep).join("/");
    if (SKIP.has(name)) continue;
    for (const line of handCuts(fs.readFileSync(file, "utf8"))) found.push(`tests/${name}:${line}`);
  }
  assert.deepEqual(found, [], "cut with between(src, from, to) from tests/helpers/between.js, which throws when a marker is missing");
});
