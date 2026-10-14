"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const context = {};
vm.createContext(context);
context.jsArg = s => JSON.stringify(String(s ?? "")); context.jsq = s => (context.esc || String)(context.jsArg(s));
vm.runInContext(fs.readFileSync("web/js/metrics-utils.js", "utf8"), context);

test("workload CPU usage is shown as percent of one core", () => {
  assert.equal(context.workloadCpuPercent(0.279), "27.9%");
  assert.equal(context.workloadCpuPercent(1.25), "125%");
  assert.equal(context.workloadCpuPercent(0), "0%");
  assert.equal(context.workloadCpuPercent(null), "0%");
});

test("several cores drop the decimal so the number stays card-width", () => {
  assert.equal(context.workloadCpuPercent(12.345), "1235%");
  assert.equal(context.workloadCpuPercent(0.9994), "99.9%");
  assert.equal(context.workloadCpuPercent(3.9), "390%");
});

test("workload memory switches to GiB rather than growing digits", () => {
  assert.equal(context.workloadMemory(738), "738 MiB");
  assert.equal(context.workloadMemory(1840), "1.8 GiB");
  assert.equal(context.workloadMemory(20480), "20 GiB");
  assert.equal(context.workloadMemory(123456), "121 GiB");
  assert.equal(context.workloadMemory(null), "0 MiB");
});

test("sizes move to TiB past 10 TiB instead of growing digits", () => {
  assert.equal(context.sizeText(986), "986 GiB");
  assert.equal(context.sizeText(25.24), "25.2 GiB");
  assert.equal(context.sizeText(12345.6), "12.1 TiB");
  assert.equal(context.sizeText(null), "0 GiB");
});

test("a used/capacity pair shares the unit the larger half needs", () => {
  assert.equal(context.sizePair(25.2, 46.8), "25.2/46.8 GiB");
  assert.equal(context.sizePair(1843.2, 2048), "1843/2048 GiB");
  assert.equal(context.sizePair(486, 1392), "486/1392 GiB");
  assert.equal(context.sizePair(12345.6, 14000), "12.1/13.7 TiB");
});

test("network rates move to Gb/s past 1000 Mb/s", () => {
  assert.deepEqual([...context.rateParts(36.14)], ["36.1", "Mb/s"]);
  assert.deepEqual([...context.rateParts(29629.6)], ["29.6", "Gb/s"]);
  assert.deepEqual([...context.ratePair(9876.5, 12345.7)], ["↓9.9 ↑12.3", "Gb/s"]);
  assert.deepEqual([...context.ratePair(1.2, 0.4)], ["↓1.2 ↑0.4", "Mb/s"]);
});

test("a byte count reads in IEC units, the largest that keeps it short", () => {
  assert.equal(context.bytesText(512), "512 B");
  assert.equal(context.bytesText(1536), "1.5 KiB");
  assert.equal(context.bytesText(48.1 * 1024 ** 2), "48.1 MiB");
  assert.equal(context.bytesText(20 * 1024 ** 3), "20 GiB");
  assert.equal(context.bytesText(3 * 1024 ** 4), "3 TiB");
  assert.equal(context.bytesText(null), "0 B");
});
