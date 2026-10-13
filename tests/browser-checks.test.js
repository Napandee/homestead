"use strict";

/* The browser checks run against whatever demo server HOMESTEAD_URL names,
   and each one is run somewhere, so none drifts from the UI unnoticed. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path");

const read = file => fs.readFileSync(file, "utf8");
const checks = fs.readdirSync("scripts").filter(f => /^(check|audit)_.*\.mjs$/.test(f)).map(f => `scripts/${f}`)
  .concat(fs.readdirSync("tests/integration").filter(f => f.endsWith(".mjs")).map(f => `tests/integration/${f}`));

test("ci_ui_checks.sh keeps a HOMESTEAD_URL that is already set", () => {
  assert.match(read("scripts/ci_ui_checks.sh"), /^export HOMESTEAD_URL="\$\{HOMESTEAD_URL:-http:\/\/127\.0\.0\.1:4173\}"$/m);
});

test("every browser check opens the demo at HOMESTEAD_URL", () => {
  const fixed = checks.filter(file => /goto\(\s*["'`]http:\/\/127\.0\.0\.1:\d+/.test(read(file)));
  assert.deepEqual(fixed, [], "read process.env.HOMESTEAD_URL, with the old address as the fallback");
});

test("every check script is run by CI, a release, or another check", () => {
  const runners = ["scripts/ci_ui_checks.sh", ...fs.readdirSync(".github/workflows").map(f => `.github/workflows/${f}`)]
    .concat(checks).map(file => [file, read(file)]);
  const orphans = checks.filter(file => file.startsWith("scripts/check_")).filter(file =>
    !runners.some(([other, text]) => other !== file && text.includes(path.basename(file))));
  assert.deepEqual(orphans, []);
});
