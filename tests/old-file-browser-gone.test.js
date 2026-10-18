"use strict";

/* Volumes and snapshots open in the two-pane file manager (filemanager.js);
   the single-pane browser it replaced is gone, and with it its own save. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs");

test("nothing in web/js still uses the old single-pane file browser", () => {
  const left = fs.readdirSync("web/js").filter(name => name.endsWith(".js"))
    .flatMap(name => [...fs.readFileSync(`web/js/${name}`, "utf8").matchAll(/\b(volumeFiles|fileBrowse|FILEVIEW|closeFiles|fileTouched|filesDismiss)\b/g)]
      .map(match => `${name}: ${match[1]}`));
  assert.deepEqual(left, []);
});
