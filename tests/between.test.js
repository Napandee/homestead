"use strict";

/* The one way a test cuts a piece out of a source file (#623): a marker that
   is gone throws, so moving a function or rewording a comment fails the test
   instead of handing it the wrong text. */
const test = require("node:test");
const assert = require("node:assert/strict");
const between = require("./helpers/between");

const src = "/* head */\nconst a = 1;\n};\nfunction b() {\n  return 2;\n}\n};\nconst c = 3;\n";

test("a piece runs from its start marker up to its end marker", () => {
  assert.equal(between(src, "const a", "function b"), "const a = 1;\n};\n");
});

test("the end marker is looked for after the start, not from the top of the file", () => {
  assert.equal(between(src, "function b", "};"), "function b() {\n  return 2;\n}\n");
});

test("through keeps the end marker", () => {
  assert.equal(between(src, "function b", "\n}\n", { through: true }), "function b() {\n  return 2;\n}\n");
});

test("null runs from the top or to the end of the file", () => {
  assert.equal(between(src, null, "const a"), "/* head */\n");
  assert.equal(between(src, "const c", null), "const c = 3;\n");
});

test("markers can be patterns", () => {
  assert.equal(between(src, /function \w+\(/, /\n\}\n/), "function b() {\n  return 2;");
});

test("a missing start marker throws and names it", () => {
  assert.throws(() => between(src, "function gone(", null), /between: "function gone\(" not found/);
  assert.throws(() => between(src, /nowhere/, null), /between: \/nowhere\/ not found/);
});

test("an end marker missing after the start throws, even when it appears earlier", () => {
  assert.throws(() => between(src, "const c", "const a"), /between: "const a" not found after "const c"/);
});
