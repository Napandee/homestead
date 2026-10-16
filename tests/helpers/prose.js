"use strict";
// The parts of a file's strings a person reads (#508): pieces with a space,
// not compared against data, with non-text HTML attributes and nested
// quoted code left out. hold() swaps those parts for markers and back.
const strings = require("./js-strings");
const DATA = /(startsWith|endsWith|includes|indexOf|===|!==|==|case|go|navigate|view)\s*\(?\s*$/;
const NOT_TEXT = /(?<![\w-])(?!title=|aria-label=|data-tip=|placeholder=|alt=)[\w-]+="[^"]*"|(?<![\w-])(?!title=|aria-label=|data-tip=|placeholder=|alt=)[\w-]+='[^']*'/g;
function hold(text, extra = []) {
  const held = [];
  const keep = s => { held.push(s); return `\u0000${held.length - 1}\u0000`; };
  // An attribute value a ${…} splits: its start ends a piece, its end begins the next.
  let t = text.replace(NOT_TEXT, keep).replace(/\b(?!title=|aria-label=|data-tip=|placeholder=|alt=)[\w-]+=["'][^"'\s]*$/, keep).replace(/^[^\s"'<>]*["'](?=[\s>])/, keep);
  for (const re of extra) t = t.replace(re, keep);
  return { text: t, back: s => s.replace(/\u0000(\d+)\u0000/g, (_, n) => held[n]) };
}
function prose(src) {
  return strings(src).filter(p => p.text.includes(" ") && !DATA.test(p.before));
}
module.exports = { prose, hold };
