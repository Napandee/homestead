"use strict";

/* Settings › Homestead: the "Health checks" cell beside Permissions (#1561).
   It says whether Homestead's own probes are this release's and, when they
   are not, shows the fix the server chose for how Homestead was installed.
   A check that failed says so; it never reads as a match. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const between = require("./helpers/between");

const source = fs.readFileSync("web/js/views-settings.js", "utf8");
const part = between(source, "/* ------------------------------------------------ Homestead's own health checks",
  "window.ownProbesPaint = ownProbesPaint;", { through: true });

function load({ admin = true, answer, fail } = {}) {
  const cell = { outerHTML: "<div id=\"ownProbesCell\"></div>" };
  const calls = [];
  const ctx = {
    window: {}, console,
    $: sel => (sel === "#ownProbesCell" ? cell : null),
    api: path => { calls.push(path); return fail ? Promise.reject(new Error(fail)) : Promise.resolve(answer); },
    esc: s => String(s ?? "").replace(/</g, "&lt;"), can: role => role === "admin" && admin,
    tip: text => `<span class="tip">${text}</span>`,
    commandBlock: text => `<pre>${text}</pre>`,
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(part, ctx);
  return { ctx, cell, calls };
}

const differs = {
  state: "differs", install: "manifest",
  detail: "The readiness check asks /healthz. This release asks /readyz?check=data.",
  effect: "With this release's check, a copy of Homestead that cannot write its data stops taking requests, even when that is every copy.",
  fix: { note: "Run once, wherever kubectl is used. It changes only these checks. Homestead restarts once to use the new checks.",
    command: "kubectl -n lab patch deployment homestead -p '{}'" },
};

test("checks that differ say so, with what the newer check changes and the command to copy", async () => {
  const { ctx, cell, calls } = load({ answer: differs });
  await ctx.ownProbesPaint();
  assert.deepEqual(calls, ["/api/self/probes"]);
  assert.match(cell.outerHTML, /^<div id="ownProbesCell">/);
  assert.match(cell.outerHTML, /Health checks/);
  assert.match(cell.outerHTML, /<b>differ from this release<\/b>/);
  assert.match(cell.outerHTML, /asks \/healthz\. This release asks \/readyz\?check=data\./);
  assert.match(cell.outerHTML, /cannot write its data stops taking requests/);
  assert.match(cell.outerHTML, /Homestead restarts once/);
  assert.match(cell.outerHTML, /<pre>kubectl -n lab patch deployment homestead -p '\{\}'<\/pre>/);
});

test("Flux or Argo CD get the values to set, not a command", async () => {
  const { ctx, cell } = load({ answer: { ...differs, install: "managed",
    fix: { note: "Homestead is updated by Flux. Set these checks where Flux reads it.", values: "readinessProbe:\n  httpGet: {}\n" } } });
  await ctx.ownProbesPaint();
  assert.match(cell.outerHTML, /Set these checks where Flux reads it/);
  assert.match(cell.outerHTML, /<pre>readinessProbe:/);
  assert.doesNotMatch(cell.outerHTML, /kubectl/);
});

test("checks that match say so in one line, with no command", async () => {
  const { ctx, cell } = load({ answer: { state: "current", detail: "They match this release's manifest.", differences: [] } });
  await ctx.ownProbesPaint();
  assert.match(cell.outerHTML, /<b>match this release<\/b><small>They match this release's manifest\.<\/small>/);
  assert.doesNotMatch(cell.outerHTML, /<pre>/);
});

test("a check that could not be made says why, and is never a match", async () => {
  const { ctx, cell } = load({ answer: { state: "unknown", detail: "Homestead's Deployment could not be read (HTTP Error 403: Forbidden).", differences: [] } });
  await ctx.ownProbesPaint();
  assert.match(cell.outerHTML, /<b>could not be checked<\/b>/);
  assert.match(cell.outerHTML, /403/);
  assert.doesNotMatch(cell.outerHTML, /match this release/);
});

test("a failed request is not a match either", async () => {
  const { ctx, cell } = load({ fail: "Network error" });
  await ctx.ownProbesPaint();
  assert.match(cell.outerHTML, /<b>could not be checked<\/b>/);
  assert.match(cell.outerHTML, /Network error/);
});

test("only an admin asks, and only an admin gets the cell", async () => {
  const { ctx, calls } = load({ admin: false, answer: differs });
  await ctx.ownProbesPaint();
  assert.deepEqual(calls, []);
  assert.match(source, /\$\{can\("admin"\) \? ownProbesCell\(\) : ""\}/);
});

/* What the server sends carries text read from the cluster (an owner's name,
   a mark, an error). With the page's own esc() and commandBlock() it is shown
   as text: markup, backticks and $(...) never become part of the page. */
test("cluster text is shown as plain text, never as markup", async () => {
  const core = fs.readFileSync("web/js/core.js", "utf8");
  const esc = between(core, "const esc = ", /\n(const|function) /);
  const block = between(source, "function commandBlock(command)", "function permissionsCell(state)");
  const hostile = "<img src=x onerror=alert(1)> `id` $(id)";
  const cell = { outerHTML: "<div id=\"ownProbesCell\"></div>" };
  const ctx = { window: {}, console, $: sel => (sel === "#ownProbesCell" ? cell : null), can: () => true,
    tip: text => `<span class="tip">${text}</span>`,
    api: () => Promise.resolve({ state: "differs", detail: `The readiness check asks ${hostile}.`,
      effect: hostile, fix: { note: `Homestead is marked as updated elsewhere (${hostile}).`, values: hostile } }) };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(`${esc}\n${block}\n${part}`, ctx);
  await ctx.ownProbesPaint();
  assert.doesNotMatch(cell.outerHTML, /<img/);
  assert.equal((cell.outerHTML.match(/&lt;img src=x onerror=alert\(1\)&gt; `id` \$\(id\)/g) || []).length, 4);
});
