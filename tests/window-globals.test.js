"use strict";
// A top-level `function f` in a classic script is a property of window, so a later
// `window.f = ...` silently replaces it (#609: helmSearch). `window.f = f` is an alias and fine.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path");

// auth.js wraps core.js's api on purpose (`const _api = window.api`).
const WRAPPED = new Set(["api"]);

test("no web/js name is both a top-level function and assigned on window", () => {
  const dir = "web/js", fns = new Map(), assigns = new Map();
  for (const f of fs.readdirSync(dir).filter(f => f.endsWith(".js"))) {
    const src = fs.readFileSync(path.join(dir, f), "utf8");
    for (const m of src.matchAll(/^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)/gm)) fns.set(m[1], f);
    for (const m of src.matchAll(/^\s*window\.([A-Za-z_$][\w$]*)\s*=(?!\s*\1\s*[;,\n])[^=]/gm)) assigns.set(m[1], [...(assigns.get(m[1]) || []), f]);
  }
  const clash = [...fns].filter(([n]) => assigns.has(n) && !WRAPPED.has(n)).map(([n, f]) => `${n}: function in ${f}, window.${n} = in ${assigns.get(n)}`);
  assert.deepEqual(clash, []);
});
