"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

function render(releases, show) {
  let painted = "";
  const ctx = { STATE: { q: "", data: { helm: releases }, platform: {} }, esc: v => String(v ?? ""), jsq: JSON.stringify,
    paint: html => { painted = html; }, UI: { pageHeader: title => `<h1>${title}</h1>` }, appAvatar: () => "",
    fmtAgo: () => "", loadAppIcons: () => {}, V: () => ({}), $: () => null, icon: () => "", can: () => true };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/views-helm.js", "utf8"), ctx);
  if (show) ctx.HELM_VIEW.show = show;
  vm.runInContext("renderHelm()", ctx);
  return painted;
}

test("an empty Helm list renders, and says when the platform's releases are hidden", () => {
  const platform = [{ name: "rancher", namespace: "cattle-system", chart: "rancher", status: "deployed", system: true }];
  assert.match(render(platform), /No Helm releases outside the platform's own yet/);
  assert.match(render([]), /No Helm releases yet/);
});

test("the Artifact Hub search still runs after the release list's filter is defined (#609)", () => {
  const calls = [], timers = [];
  const host = { innerHTML: "" };
  const ctx = { STATE: { q: "", data: {}, platform: {} }, esc: v => String(v ?? ""), jsq: JSON.stringify,
    paint: () => {}, UI: {}, appAvatar: () => "", fmtAgo: () => "", loadAppIcons: () => {}, V: () => ({}), icon: () => "", can: () => true,
    $: sel => (sel === "#hi_q" ? { value: " grafana " } : host),
    api: async url => { calls.push(url); return []; },
    setTimeout: fn => { timers.push(fn); return timers.length; }, clearTimeout: () => {} };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/views-helm.js", "utf8"), ctx);
  ctx.helmSearchSoon();
  assert.equal(timers.length, 1);
  timers[0]();
  assert.deepEqual(calls, ["/api/helm/search?q=grafana"]);
});
