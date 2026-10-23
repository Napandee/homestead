"use strict";

/* Homestead's health card says it is "checked every 15 seconds while this is
   open". The refresh has to fire while Settings shows the section holding the
   card, keep going after one failed read, and stop once Settings is left. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const between = require("./helpers/between");

const source = fs.readFileSync("web/js/views-settings.js", "utf8");
const sections = between(source, "const SETTINGS_SECTIONS = [", "function settingsTab(");
const paint = between(source, "const HEALTH_TONE", "window.selfHealthPaint = selfHealthPaint;");

const HEALTH = { loops: [], api: { ok: true, ms: 20 }, version: "1.0.0", replicas: { desired: 1, pods: [] }, leader: true };

function settings({ view = "settings", section = "homestead", answers = [] } = {}) {
  const timers = [], reads = [];
  const card = { innerHTML: "" };
  const ctx = {
    window: {}, STATE: { view, data: {} }, console,
    $: sel => (sel === "#selfHealthCard" ? card : null),
    api: path => { reads.push(path); const next = answers.length ? answers.shift() : HEALTH; return next instanceof Error ? Promise.reject(next) : Promise.resolve(next); },
    esc: s => String(s ?? ""), can: () => true, fmtAgo: s => `${s} s`,
    UI: { moduleHeader: title => `<h3>${title}</h3>`, callout: (tone, title, text) => `<p>${title}: ${text}</p>` },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: () => { },
  };
  ctx.window = ctx;
  ctx.settingsTab = () => ctx.__section;
  ctx.__section = section;
  vm.createContext(ctx);
  vm.runInContext(`${sections}\n${paint}\nwindow.selfHealthPaint = selfHealthPaint;`, ctx);
  const fire = async () => { const t = timers.shift(); assert.ok(t, "a refresh is scheduled"); assert.equal(t.ms, 15000); t.fn(); await new Promise(r => setImmediate(r)); };
  return { ctx, reads, timers, card, fire };
}

test("the health card is read again 15 s later while its section is shown", async () => {
  const s = settings();
  await s.ctx.selfHealthPaint();
  assert.equal(s.reads.length, 1);
  await s.fire();
  assert.equal(s.reads.length, 2);
});

test("one failed read does not stop the refresh", async () => {
  const s = settings({ answers: [new Error("timed out"), HEALTH] });
  await s.ctx.selfHealthPaint();
  assert.match(s.card.innerHTML, /Status unavailable: timed out/);
  await s.fire();
  assert.equal(s.reads.length, 2);
  assert.doesNotMatch(s.card.innerHTML, /Status unavailable/);
});

test("the refresh stops once Settings is left", async () => {
  const s = settings();
  await s.ctx.selfHealthPaint();
  s.ctx.STATE.view = "apps";
  await s.fire();
  assert.equal(s.reads.length, 1);
  assert.equal(s.timers.length, 0, "nothing more is scheduled once Settings is left");
});

test("on another section it waits, and reads again once the section is back", async () => {
  const s = settings();
  await s.ctx.selfHealthPaint();
  s.ctx.__section = "updates";
  await s.fire();
  assert.equal(s.reads.length, 1);
  s.ctx.__section = "homestead";
  await s.fire();
  assert.equal(s.reads.length, 2);
});
