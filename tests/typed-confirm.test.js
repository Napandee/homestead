"use strict";
// One typed confirmation for the actions that can't be undone (#489): the
// exact text shown, the button disabled until it matches, Enter held too.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

function context() {
  const elements = {};
  const c = { esc: v => String(v ?? "").replace(/[&<>"']/g, ch => `&#${ch.charCodeAt(0)};`),
    document: { getElementById: id => elements[id] || null, addEventListener() {} } };
  c.window = c; vm.createContext(c);
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), c);
  return { c, elements };
}

test("the field shows the exact text and is tied to its label", () => {
  const { c } = context();
  const html = c.UI.typedConfirm({ id: "vd_confirm", expect: "win11", button: "vd_go" });
  assert.match(html, /<label for="vd_confirm">Type <b class="mono">win11<\/b> to confirm<\/label>/);
  assert.match(html, /<input id="vd_confirm"[^>]*data-typed-expect="win11"[^>]*data-typed-button="vd_go"/);
  assert.match(html, /autocomplete="off"/);
});

test("the button stays disabled until the text matches, trimmed and case-sensitive", () => {
  const { c, elements } = context();
  const button = elements.vd_go = { disabled: true };
  const input = { value: "", dataset: { typedExpect: "win11", typedButton: "vd_go" } };
  elements.vd_confirm = input;
  for (const [typed, ok] of [["win1", false], ["WIN11", false], [" win11 ", true], ["win11", true], ["win11x", false]]) {
    input.value = typed; c.typedConfirmSync(input);
    assert.equal(button.disabled, !ok, typed);
    assert.equal(c.typedConfirmMatches("vd_confirm"), ok, typed);
  }
});

test("Enter does nothing until the text matches", () => {
  const { c } = context();
  const input = { value: "win1", dataset: { typedExpect: "win11", typedButton: "vd_go" } };
  let stopped = false;
  c.typedConfirmKey({ key: "Enter", target: input, preventDefault() { stopped = true; }, stopPropagation() {} });
  assert.equal(stopped, true);
  stopped = false; input.value = "win11";
  c.typedConfirmKey({ key: "Enter", target: input, preventDefault() { stopped = true; }, stopPropagation() {} });
  assert.equal(stopped, false);
});

test("no dialog builds its own typed confirmation", () => {
  const left = [];
  for (const file of fs.readdirSync("web/js").filter(f => f.endsWith(".js") && !["ui.js", "demo.js"].includes(f))) {
    fs.readFileSync(`web/js/${file}`, "utf8").split("\n").forEach((line, i) => {
      if (/[Tt]ype <(b|span)[^>]*>[^<]*<\/(b|span)>[^<]{0,40}to confirm|[Tt]ype the [A-Za-z' ]+name|[Tt]ype its name|to confirm( [a-z]+)?<\/label>|UI\.field\(`Type /.test(line)) left.push(`${file}:${i + 1}`);
    });
  }
  assert.deepEqual(left, [], "use UI.typedConfirm");
});
