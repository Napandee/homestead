"use strict";
// The text of a JavaScript file a person can read: each string literal and
// each piece of a template outside its ${…}, with ${…} itself walked too, so
// a tip("…") inside a template is found. Comments and code are skipped.
// Each piece comes with where it starts and the code just before it.
module.exports = function strings(src) {
  const out = [];
  let i = 0;
  const regexCan = () => { const before = src.slice(0, i).replace(/\s+$/, ""); return /(^|[=(,:;!&|?{}[+\-*%<>~^]|\breturn|\btypeof|\bcase)$/.test(before); };
  function quoted(q) {
    const start = ++i;
    while (i < src.length && src[i] !== q) { if (src[i] === "\\") i++; if (src[i] === "\n") break; i++; }
    out.push({ text: src.slice(start, i), at: start, before: src.slice(Math.max(0, start - 40), start - 1) });
    i++;
  }
  function template() {
    i++;
    let start = i;
    while (i < src.length && src[i] !== "`") {
      if (src[i] === "\\") { i += 2; continue; }
      if (src[i] === "$" && src[i + 1] === "{") {
        out.push({ text: src.slice(start, i), at: start, before: src.slice(Math.max(0, start - 40), start) });
        i += 2; code(true); start = i; continue;
      }
      i++;
    }
    out.push({ text: src.slice(start, i), at: start, before: src.slice(Math.max(0, start - 40), start) });
    i++;
  }
  function code(inner) {
    let depth = 0;
    while (i < src.length) {
      const c = src[i], d = src[i + 1];
      if (c === "/" && d === "/") { while (i < src.length && src[i] !== "\n") i++; continue; }
      if (c === "/" && d === "*") { i = src.indexOf("*/", i + 2); i = i < 0 ? src.length : i + 2; continue; }
      if (c === "/" && regexCan()) { i++; let cls = false; while (i < src.length && (cls || src[i] !== "/") && src[i] !== "\n") { if (src[i] === "\\") i++; else if (src[i] === "[") cls = true; else if (src[i] === "]") cls = false; i++; } i++; continue; }
      if (c === '"' || c === "'") { quoted(c); continue; }
      if (c === "`") { template(); continue; }
      if (c === "{") depth++;
      if (c === "}") { if (inner && depth === 0) { i++; return; } depth--; }
      i++;
    }
  }
  code(false);
  return out;
};
