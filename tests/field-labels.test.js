"use strict";

/* A field's label names its control: a screen reader reads it, and
   clicking it focuses the field. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

const ctx = { esc: v => String(v ?? "").replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`), document: {} };
ctx.window = ctx; vm.createContext(ctx);
vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), ctx);

test("UI.field links its label to the control's id", () => {
  assert.match(ctx.UI.field("Name", '<input id="d_name">'), /<label for="d_name">Name<\/label><input id="d_name">/);
});

test("UI.field gives a control without an id one, and skips hidden inputs", () => {
  const html = ctx.UI.field("Size", '<input type="hidden" name="k"><select data-id="x"><option>1</option></select>');
  const id = (html.match(/<label for="([^"]+)">/) || [])[1];
  assert.ok(id, html);
  assert.match(html, new RegExp(`<select id="${id}" data-id="x">`));
});

test("a setting row names its control with its label", () => {
  const html = vm.runInContext("settingRow", ctx)("Refresh every", "Seconds between updates", '<input id="optRefresh" type="number">');
  assert.match(html, /<b id="optRefresh-label">Refresh every<\/b>/);
  assert.match(html, /<input aria-labelledby="optRefresh-label" id="optRefresh" type="number">/);
});
