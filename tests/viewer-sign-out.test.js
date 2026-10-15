"use strict";

/* A viewer may not act on the cluster, so style.css switches their primary
   and danger buttons off. Signing out is not acting on the cluster: whatever
   the role, the buttons that sign out must stay clickable. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");

const css = fs.readFileSync("web/style.css", "utf8");
// Each body[data-role="viewer"] rule that turns pointer events off, as the
// button classes it needs and the attributes that exempt a button from it.
const switchedOff = [...css.matchAll(/((?:body\[data-role="viewer"\][^{,]*,\s*)*body\[data-role="viewer"\][^{,]*)\{[^}]*pointer-events:\s*none/g)]
  .flatMap(m => m[1].split(","))
  .map(selector => ({
    classes: [...selector.matchAll(/\.btn((?:\.[a-z-]+)*)/g)].flatMap(c => c[1].split(".").filter(Boolean)),
    exempt: [...selector.matchAll(/:not\(\[([a-z-]+)\]\)/g)].map(a => a[1]),
  }));

const buttons = fs.readdirSync("web/js").filter(f => f.endsWith(".js"))
  .flatMap(file => [...fs.readFileSync(`web/js/${file}`, "utf8").matchAll(/<button\b[^>]*onclick="(doLogout|signOutEverywhere)\(\)"[^>]*>/g)]
    .map(m => ({ file, action: m[1], tag: m[0] })));

test("the stylesheet's viewer rules are found", () => {
  assert.ok(switchedOff.some(rule => rule.classes.includes("danger")), "viewer rule for .btn.danger");
});

test("a viewer can click every button that signs out", () => {
  assert.ok(buttons.some(b => b.action === "doLogout"), "a Sign out button exists");
  for (const { file, action, tag } of buttons) {
    const classes = (tag.match(/class="([^"]*)"/) || [, ""])[1].split(/\s+/);
    for (const rule of switchedOff) {
      const caught = rule.classes.length && rule.classes.every(c => classes.includes(c))
        && !rule.exempt.some(attr => new RegExp(`\\b${attr}\\b`).test(tag));
      assert.ok(!caught, `${action} in ${file} is switched off for viewers by .btn.${rule.classes.join(".")}`);
    }
  }
});
