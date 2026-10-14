"use strict";
// One name for each thing (#508): what runs from an image is an app (with
// "container" for the process inside); "workload" is Kubernetes' word. It
// stays only where it names a networking feature that serves VMs too.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs");

const KEPT = new Set(["views-network.js", "views-firewall.js"]);
// A word a person reads: not a property, a variable or a path.
const WORD = /(?<![.{\w$/-])[Ww]orkloads?(?![\w(/-]|\.\w)/;
const NAMED = /workload (VIPs?|Services?|policies|firewall)|Workload VIPs|Expose workload|Kubernetes workloads/i;

const text = line => [...line.matchAll(/"([^"]*)"|'([^']*)'|`([^`]*)`|>([^<>]+)</g)].map(m => m.slice(1).find(Boolean) || "");

test("no screen says workload, except a networking feature that serves VMs too", () => {
  const left = [];
  for (const file of [...fs.readdirSync("web/js").filter(f => f.endsWith(".js") && f !== "demo.js" && !KEPT.has(f)).map(f => `web/js/${f}`), "web/index.html"])
    fs.readFileSync(file, "utf8").split("\n").forEach((line, i) => {
      if (/^\s*(\/\/|\/\*|\*)/.test(line)) return;
      if (text(line).some(t => t.includes(" ") && WORD.test(t.replace(NAMED, "")))) left.push(`${file.split("/").pop()}:${i + 1}`);
    });
  assert.deepEqual(left, [], "say app, or apps and VMs");
});
