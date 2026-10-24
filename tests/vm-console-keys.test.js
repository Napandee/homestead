"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs");
const between = require("./helpers/between");

const source = fs.readFileSync("web/js/vm-console.js", "utf8");
const strokes = new Function("VMC", "navigator", "localStorage",
  between(source, "const KEYSYM = ", "window.vmKeyLayoutChanged") + "return vmKeyStrokes;")(
  {}, { language: "en-US" }, { getItem: () => null });
// What a guest sees: the keys pressed, Shift by name.
const typed = (text, layout) => [...text].map(ch => strokes(ch, layout).filter(([, , down]) => down)
  .map(([keysym, code]) => code || `keysym:${keysym}`).join("+"));

test("pasting an upper-case letter or a shifted symbol holds Shift round its key", () => {
  assert.deepEqual(typed("Ab_", "us"), ["ShiftLeft+KeyA", "KeyB", "ShiftLeft+Minus"]);
  const shifted = strokes("A", "us");
  assert.deepEqual(shifted.map(([, code, down]) => `${code}:${down ? "down" : "up"}`),
    ["ShiftLeft:down", "KeyA:down", "KeyA:up", "ShiftLeft:up"]);
});

test("a UK guest gets the keys its own layout types each character with", () => {
  assert.deepEqual(typed("\\|#~@\"£", "gb"),
    ["IntlBackslash", "ShiftLeft+IntlBackslash", "Backslash", "ShiftLeft+Backslash", "ShiftLeft+Quote", "ShiftLeft+Digit2", "ShiftLeft+Digit3"]);
  assert.deepEqual(typed("\\@\"", "us"), ["Backslash", "ShiftLeft+Digit2", "ShiftLeft+Quote"]);
});

test("Enter, Tab and space are their keys; a character no key types goes as itself", () => {
  assert.deepEqual(typed("\n\t ", "us"), ["Enter", "Tab", "Space"]);
  assert.deepEqual(typed("é", "us"), [`keysym:${0xe9}`]);
});
