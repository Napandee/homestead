"use strict";
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs");

/* UI.actions is a dialog's sticky footer: negative margins, a bar and a
   backdrop. In a callout - a host's Drive warning, IOMMU is off - it has to
   be a plain row inside the callout, or it spills past its edges as a dark box. */
test("actions inside a callout are a row of the callout, not a dialog footer", () => {
  const css = fs.readFileSync("web/style.css", "utf8");
  const rule = css.match(/\.ui-callout \.ui-actions\{([^}]*)\}/);
  assert.ok(rule, "a rule for actions inside a callout");
  for (const part of ["position:static", "margin:10px 0 0!important", "padding:0", "background:none", "backdrop-filter:none"])
    assert.ok(rule[1].replace(/\s+/g, "").includes(part.replace(/\s+/g, "")), part);
});
