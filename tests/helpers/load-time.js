"use strict";
// The shared time helpers from core.js (#503), for a test that renders a
// view without the rest of core.js.
const fs = require("node:fs"), vm = require("node:vm");
module.exports = ctx => {
  const core = fs.readFileSync("web/js/core.js", "utf8");
  if (!ctx.esc) ctx.esc = x => String(x ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;");
  if (!ctx.Intl) ctx.Intl = Intl;
  vm.runInContext(core.slice(core.indexOf("const fmtUp"), core.indexOf("const upChip")), ctx);
};
