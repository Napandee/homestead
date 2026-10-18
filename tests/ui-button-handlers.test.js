"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path");

/* UI.button escapes the handler it is given, so a value in it is written
   with jsArg; jsq (already escaped) there is escaped twice, and the browser
   hands the handler &quot; instead of a quote: the button does nothing. */
test("no UI.button handler escapes its values twice", () => {
  const found = [];
  for (const file of fs.readdirSync("web/js").filter(f => f.endsWith(".js"))) {
    const source = fs.readFileSync(path.join("web/js", file), "utf8");
    for (const call of source.match(/UI\.button\((?:[^`]|`[^`]*`)*?\)/g) || []) {
      if (/\bjsq\(|esc\(JSON\.stringify/.test(call)) found.push(`${file}: ${call.slice(0, 80)}`);
    }
  }
  assert.deepEqual(found, []);
});
