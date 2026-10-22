// #543: one way to write a small coloured label, UI.chip(label, tone). The
// hand-written `class="pill…"` and `class="tag…"` chips move to it a page at a
// time, and this count keeps them from growing back. Like `.note` in
// design-rules, it only shrinks: when you remove some, lower the number here in
// the same change. Its own file, so the migration PRs don't share one with
// design-rules' other counts.
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

// Hand-written chips left, over the files design-rules scans. Not counted:
// a class built in a template (`${row ? "tag" : "pill"}`), a className
// assignment, and web/index.html; the migrations take those by hand.
const COUNTS = { pill: 194, tag: 228 };
// A migrated `web/js/…` file → how many non-interactive chips it still writes
// by hand on purpose (usually 0). Buttons and links styled as chips don't count.
const MIGRATED = {};

const ROOT = path.join(__dirname, "..");
const WEB = path.join(ROOT, "web", "js");
// demo.js is made-up data, not markup; ui.js defines the components.
const FILES = fs.readdirSync(WEB).filter(f => f.endsWith(".js") && !["demo.js", "ui.js"].includes(f))
  .map(f => ({ name: `web/js/${f}`, text: fs.readFileSync(path.join(WEB, f), "utf8") }));
const lineOf = (text, index) => text.slice(0, index).split("\n").length;

// Where `class="pill …"` / `class="tag …"` starts in a text (not `tag-list`).
const chipsIn = (text, kind = "(?:pill|tag)") => [...text.matchAll(new RegExp(`class="${kind}(?=[\\s"])`, "g"))].map(m => m.index);
// The chips a text writes by hand: not a <button> or <a> styled as one.
const handWritten = text => chipsIn(text).filter(index => {
  const opened = [...text.slice(0, index).matchAll(/<(\w+)/g)].pop();
  return !["button", "a"].includes(opened ? opened[1] : "");
});

test("the counter sees chips, not classes that start with pill or tag", () => {
  const text = '<div class="tag-list"><span class="tag ok">a</span><span class="pill">b</span><i class="pill-row"></i></div>';
  assert.deepStrictEqual(chipsIn(text, "tag").length, 1);
  assert.deepStrictEqual(chipsIn(text, "pill").length, 1);
});

test("buttons and links styled as chips are not hand-written chips", () => {
  const text = '<span class="tag">a</span><button class="tag movable" onclick="x()">b</button><a class="pill" href="#">c</a><span class="pill ok">d</span>';
  assert.strictEqual(handWritten(text).length, 2);
});

test("hand-written pills and tags only shrink", () => {
  for (const kind of Object.keys(COUNTS)) {
    const found = [];
    for (const file of FILES) {
      for (const index of chipsIn(file.text, kind)) found.push(`${file.name}:${lineOf(file.text, index)}`);
    }
    const limit = COUNTS[kind];
    assert.ok(found.length <= limit,
      `${found.length} hand-written ${kind} chips (at most ${limit}); use UI.chip(label, tone). ` +
      `When you remove some, lower the number in the same change.\n  ${found.slice(-5).join("\n  ")}`);
  }
});

test("a migrated file writes no chip by hand", () => {
  for (const [name, kept] of Object.entries(MIGRATED)) {
    const file = FILES.find(f => f.name === name);
    assert.ok(file, `${name} is in MIGRATED but is not a scanned file`);
    const found = handWritten(file.text).map(index => `${name}:${lineOf(file.text, index)}`);
    assert.ok(found.length <= kept,
      `${name} writes ${found.length} chips by hand (at most ${kept}); use UI.chip(label, tone):\n  ${found.join("\n  ")}`);
  }
});

test("design.md names the chip counter", () => {
  const doc = fs.readFileSync(path.join(ROOT, "docs", "design.md"), "utf8");
  assert.ok(doc.includes("tests/chip-count.test.js"), "docs/design.md: list tests/chip-count.test.js with the counts that only shrink");
});
