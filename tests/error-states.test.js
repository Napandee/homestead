"use strict";

/* A failed load says it failed and offers Try again; in a dialog the footer
   stays, so Close still works. An empty list says so in a sentence. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

const ctx = { esc: v => String(v ?? "").replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`), icon: () => "", document: {} };
ctx.window = ctx; vm.createContext(ctx);
vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), ctx);

test("UI.error shows the reason as a failure with Try again", () => {
  const html = ctx.UI.error(new Error("connection refused"), { retry: "helmRelease('lab','frigate')" });
  assert.match(html, /ui-callout bad/);
  assert.match(html, /connection refused/);
  assert.match(html, /onclick="helmRelease\(&#39;lab&#39;,&#39;frigate&#39;\)"[^>]*>.*Try again/s);
  assert.doesNotMatch(html, /class="empty/);
});

test("in a dialog, UI.error keeps a footer with Close", () => {
  const html = ctx.UI.error(new Error("timed out"), { retry: "lhSnaps('pvc-1')", dialog: true });
  assert.match(html, /class="ui-actions/);
  assert.match(html, /data-dialog-dismiss="true"[^>]*>Close</);
  assert.match(html, /Try again/);
});

test("UI.empty is one sentence, with the next step when there is one", () => {
  assert.match(ctx.UI.empty("No snapshots yet."), /<div class="ui-empty">No snapshots yet\.<\/div>/);
  const html = ctx.UI.empty("No snapshots yet.", { action: ctx.UI.button("Take one now", "lhSnap('pvc-1')") });
  assert.match(html, /No snapshots yet\.[\s\S]*Take one now/);
});

test("no failure is drawn as an empty list", () => {
  const left = [];
  for (const file of fs.readdirSync("web/js").filter(f => f.endsWith(".js"))) {
    fs.readFileSync(`web/js/${file}`, "utf8").split("\n").forEach((line, i) => {
      if (/class="empty[^"]*">\$\{esc\((e|err|error)\.message\)/.test(line)) left.push(`${file}:${i + 1}`);
    });
  }
  assert.deepEqual(left, [], "use UI.error(e, { retry })");
});

test("a Data Protection job or group with a missing field shows it at the field", () => {
  const source = fs.readFileSync("web/js/views-protect.js", "utf8");
  assert.doesNotMatch(source, /toast\("name is required"|toast\("pick at least one group|toast\("give the group a name/);
  assert.match(source, /fieldError\("#lj_name"/);
  assert.match(source, /fieldError\("#lg_name"/);
});
