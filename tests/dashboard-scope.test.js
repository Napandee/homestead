"use strict";

/* With "All clusters" picked, the Dashboard still shows only this cluster:
   its subtitle has to say so, and point to the pages that combine them. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs");

const source = fs.readFileSync("web/js/views-overview.js", "utf8");
const start = source.indexOf("function dashboardScope(");
assert.ok(start >= 0, "views-overview.js has dashboardScope()");
const body = source.slice(start, source.indexOf("\n}\n", start) + 2);

function scope(all, nodes = 3) {
  const esc = s => String(s);
  const FLEET = { view: { linked: true, members: [{ name: "Main site", self: true }, { name: "Branch office" }] } };
  return new Function("fleetAll", "FLEET", "esc", `${body}; return dashboardScope;`)(() => all, FLEET, esc)({ nodes_total: nodes });
}

test("with All clusters picked, the dashboard says it shows this cluster only", () => {
  const text = scope(true);
  assert.match(text, /Main site only/);
  assert.match(text, /3 nodes/);
  assert.match(text, /go\('nodes'\)/, "it links to Nodes, which shows every cluster");
});

test("on one cluster the subtitle is as before", () => {
  assert.equal(scope(false), "Live health, capacity and placement across 3 nodes");
  assert.equal(scope(false, 1), "Live health, capacity and placement across 1 node");
});
