"use strict";

/* The jobs tray asks the server every 3 s while a job runs (#705). A hidden tab
   does not need that, and nor does a job whose state has not moved for a
   minute; anything the person does snaps it back to 3 s. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync("web/js/operations.js", "utf8");

// Any element the tray paints: every property is callable or settable.
const element = () => new Proxy(function () {}, {
  get: (target, key) => key === "classList" ? {add() {}, remove() {}, toggle() {}} : element(),
  set: () => true,
  apply: () => element(),
});

function tray({hidden = false, jobs}) {
  let now = 0, nextId = 1;
  const timers = new Map(), listeners = {}, calls = [];
  const documentStub = {
    hidden,
    addEventListener: (name, fn) => { (listeners[name] ||= []).push(fn); },
  };
  const context = {
    console, JSON, Math, Number, String, Date: {now: () => now},
    document: documentStub,
    ME: {name: "ada"},
    STATE: {data: {}},
    $: () => element(), esc: String, jsq: JSON.stringify, icon: () => "",
    UI: new Proxy({}, {get: () => () => ""}),
    api: async path => { calls.push({path, at: now}); return jobs(now); },
    setTimeout: (fn, ms) => { const id = nextId++; timers.set(id, {fn, at: now + ms}); return id; },
    clearTimeout: id => timers.delete(id),
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(source, context);
  const settle = () => new Promise(resolve => setImmediate(resolve));
  return {
    context, calls, documentStub,
    pending: () => timers.size,
    listeners: name => (listeners[name] || []).length,
    async start() { await context.startOperationChecks(); await settle(); },
    // Advance the clock in 1 s steps, firing any timer that is due.
    async advance(ms) {
      for (const end = now + ms; now < end;) {
        now += 1000;
        for (const [id, timer] of [...timers]) {
          if (timer.at <= now) { timers.delete(id); await timer.fn(); await settle(); }
        }
      }
    },
    async visibility(isHidden) {
      documentStub.hidden = isHidden;
      for (const fn of listeners.visibilitychange || []) fn();
      await settle();
    },
  };
}

const running = (progress = 10) => [{id: "j1", kind: "workload-copy", status: "running", progress, title: "Copy"}];

test("a hidden tab asks at most once a minute, even with a job running", async () => {
  const t = tray({hidden: true, jobs: () => running()});
  await t.start();
  const before = t.calls.length;
  await t.advance(60_000);
  assert.ok(t.calls.length - before <= 1, `${t.calls.length - before} calls in a hidden minute`);
});

test("a tab that goes hidden slows down, and asks at once when it is shown again", async () => {
  const t = tray({jobs: () => running()});
  await t.start();
  await t.visibility(true);
  const hiddenFrom = t.calls.length;
  await t.advance(30_000);
  assert.ok(t.calls.length - hiddenFrom <= 1, "no 3 s polling while hidden");
  const shownAt = t.calls.length;
  await t.visibility(false);
  assert.equal(t.calls.length, shownAt + 1, "showing the tab asks straight away");
});

test("a running job that has not changed for a minute is asked every 10 s, not 3 s", async () => {
  const t = tray({jobs: () => running(10)});
  await t.start();
  await t.advance(70_000);
  const from = t.calls.length;
  await t.advance(30_000);
  const calls = t.calls.length - from;
  assert.ok(calls >= 2 && calls <= 4, `${calls} calls in 30 s once settled`);
});

test("a change in the job snaps back to every 3 s", async () => {
  let progress = 10;
  const t = tray({jobs: () => running(progress)});
  await t.start();
  await t.advance(70_000);
  progress = 20;
  await t.advance(12_000);      // the next settled read sees the change
  const from = t.calls.length;
  await t.advance(15_000);
  assert.ok(t.calls.length - from >= 4, `${t.calls.length - from} calls in 15 s after a change`);
});

test("something the person does (refreshOperations(true)) snaps back to every 3 s", async () => {
  const t = tray({jobs: () => running(10)});
  await t.start();
  await t.advance(70_000);
  await t.context.refreshOperations(true);
  const from = t.calls.length;
  await t.advance(15_000);
  assert.ok(t.calls.length - from >= 4, `${t.calls.length - from} calls in 15 s after an action`);
});

test("with nothing running the tray still asks every 15 s", async () => {
  const t = tray({jobs: () => [{id: "j1", status: "succeeded", title: "Copy"}]});
  await t.start();
  const from = t.calls.length;
  await t.advance(60_000);
  assert.equal(t.calls.length - from, 4);
});

test("starting the tray again does not add a second visibility listener", async () => {
  const t = tray({jobs: () => running()});
  await t.start();
  await t.start();
  assert.equal(t.listeners("visibilitychange"), 1);
});

test("hiding the tab while a 3 s poll is pending leaves exactly one poll waiting", async () => {
  const t = tray({jobs: () => running()});
  await t.start();
  assert.equal(t.pending(), 1, "the 3 s poll is waiting");
  await t.visibility(true);
  assert.equal(t.pending(), 1, "replaced by the hidden-tab poll, not added to it");
});
