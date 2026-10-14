"use strict";
// Sizes are IEC (#502): Homestead divides by 1024, as Kubernetes' 10Gi and
// Longhorn do, so it says KiB, MiB, GiB and TiB. Link speeds stay in bits per
// second (Mb/s, Gb/s); a copy's speed is bytes, so MiB/s.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs");

const files = fs.readdirSync("web/js").filter(f => f.endsWith(".js") && f !== "demo.js");
const lines = files.flatMap(f => fs.readFileSync(`web/js/${f}`, "utf8").split("\n").map((line, i) => [`${f}:${i + 1}`, line]));

test("no view labels a size in decimal units", () => {
  // A unit word after a number, a ${…} or a quote: " GB", "(GB)", "GB/s".
  const decimal = /(\d|\}|["'`(]|\s)(KB|MB|GB|TB)(?![a-zA-Z])/;
  const left = lines.filter(([, line]) => decimal.test(line.replace(/\/\/.*$/, ""))).map(([at]) => at);
  assert.deepEqual(left, [], "use KiB, MiB, GiB or TiB, or bytesText()/sizeText()");
});

test("a size input says GiB", () => {
  const left = lines.filter(([, line]) => /Size \(GB\)|Size GB|\(GB\)/.test(line)).map(([at]) => at);
  assert.deepEqual(left, []);
});

test("a link speed has one spelling, Mb/s", () => {
  assert.deepEqual(lines.filter(([, line]) => /Mbit\/s|Mbps\b/.test(line)).map(([at]) => at), []);
});
