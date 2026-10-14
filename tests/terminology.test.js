"use strict";
// One name for each thing (#508). What runs from an image is an app (with
// "container" for the process inside); "workload" is Kubernetes' word and
// stays only where it names a networking feature that serves VMs too. A
// machine in the cluster is a host; "node" stays only for Kubernetes' and
// Longhorn's own terms.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs");
const { prose, hold } = require("./helpers/prose");

const files = fs.readdirSync("web/js").filter(f => f.endsWith(".js") && f !== "demo.js");
const html = fs.readFileSync("web/index.html", "utf8").replace(/<script[\s\S]*?<\/script>/g, "");
const pieces = files.flatMap(f => prose(fs.readFileSync(`web/js/${f}`, "utf8")).map(p => ({ ...p, file: f })))
  .concat([...html.matchAll(/>([^<>]*\s[^<>]*)</g)].map(m => ({ text: m[1], file: "index.html" })));
const where = p => `${p.file}: ${p.text.trim().slice(0, 70)}`;

const KEPT_FILES = new Set(["views-network.js", "views-firewall.js"]);
const NAMED = /workload (VIPs?|Services?|policies|firewall)|Workload VIPs|Expose workload|Kubernetes workloads/gi;
const WORKLOAD = /(?<![\w$/-])[Ww]orkloads?(?![\w(/-])/;

test("no screen says workload, except a networking feature that serves VMs too", () => {
  const left = pieces.filter(p => !KEPT_FILES.has(p.file) && WORKLOAD.test(hold(p.text, [NAMED]).text)).map(where);
  assert.deepEqual(left, [], "say app, or apps and VMs");
});

const NODE_TERMS = /node (labels?|selectors?|objects|agents?|probes?|password|tags?|disks)|Node (filesystem|probe|role|tags)|Longhorn nodes?|Harvester nodes?|management nodes|Management nodes|nodes? tagged|nodes with (a|every) tag|No node has a tag|(?:on|to|across) (?:one|many|several) nodes?(?= at a time| through|\.|,| using| but)|RWO · one node|RWX · many nodes|single-node \(RWO\)|pods of a node|failed node's volumes|node-down/g;
const NODE = /(?<![\w$/-])[Nn]odes?(?![\w(/-])/;

test("a machine in the cluster is a host on screen; node only for Kubernetes' and Longhorn's own terms", () => {
  const left = pieces.filter(p => NODE.test(hold(p.text, [NODE_TERMS]).text)).map(where);
  assert.deepEqual(left, [], "say host");
});

test("a VM's power and moves are Shut down, Force off and Move to another host; its CPUs are vCPUs", () => {
  const vm = ["views-vms.js", "vm-hardware.js", "views-lifecycle.js"].map(f => [f, fs.readFileSync(`web/js/${f}`, "utf8")]);
  const said = (re, files = vm) => files.flatMap(([f, src]) => prose(src).filter(p => re.test(p.text)).map(p => `${f}: ${p.text.trim().slice(0, 60)}`));
  assert.deepEqual(said(/Force stop|Move host\b|^Migrate\b|>Migrate</), []);
  assert.deepEqual(said(/CPU cores|virtual CPUs/), []);
  assert.deepEqual(said(/\} cores?\b|\} core\$\{/, vm.filter(([f]) => f === "views-vms.js")), [], "a VM's CPUs are vCPUs");
});
