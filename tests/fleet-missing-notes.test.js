// The view of all clusters says what a linked Homestead's absence means (#705):
// not answered yet, an error, or not answering; each once per state.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

function fleet() {
  const toasts = [];
  const ctx = {window: {}, toast: (msg, kind) => toasts.push([msg, kind]), document: {addEventListener() {}},
               STATE: {}, $: () => null, location: {href: ""}, FLEET_LISTS: {}, console};
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/fleet.js", "utf8"), ctx);
  return {note: rows => ctx.fleetNoteMissing(encodeURIComponent(JSON.stringify(rows))), toasts};
}

test("a Homestead that has not answered yet is said so, quietly, once", () => {
  const {note, toasts} = fleet();
  note([{id: "office", name: "Office", state: "waiting"}]);
  note([{id: "office", name: "Office", state: "waiting"}]);
  assert.equal(toasts.length, 1);
  assert.match(toasts[0][0], /Office has not answered yet/);
  assert.notEqual(toasts[0][1], "warn");
});

test("one that keeps missing is then said not to be answering", () => {
  const {note, toasts} = fleet();
  note([{id: "office", name: "Office", state: "waiting"}]);
  note([{id: "office", name: "Office", state: "not answering", age_s: 95}]);
  assert.equal(toasts.length, 2);
  assert.match(toasts[1][0], /Office is not answering/);
  assert.match(toasts[1][0], /from 1 min ago/);
  assert.equal(toasts[1][1], "warn");
});

test("an old Homestead without states is still named as not answering", () => {
  const {note, toasts} = fleet();
  note([{id: "office", name: "Office", error: "timed out"}]);
  assert.match(toasts[0][0], /Office is not answering/);
});

test("one that is slow but has rows shown says how old they are, without contradicting itself", () => {
  const {note, toasts} = fleet();
  note([{id: "office", name: "Office", state: "waiting", age_s: 15}]);
  assert.match(toasts[0][0], /Office is slow to answer; its rows shown are from 15 s ago/);
  assert.doesNotMatch(toasts[0][0], /appear when it does/);
});

test("an error is said once, with the member named once", () => {
  const {note, toasts} = fleet();
  note([{id: "office", name: "Office", state: "error", error: "it did not answer"}]);
  assert.equal(toasts[0][0].match(/Office/g).length, 1);
  assert.match(toasts[0][0], /Office could not answer \(it did not answer\)/);
});

function architecture() {
  const ctx = {console, Date, Map, Set, Promise, document: {addEventListener() {}}, STATE: {data: {}}, FLEET_LISTS: {},
    toast() {}, $: () => null, location: {href: ""},
    esc: x => String(x).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
    UI: {callout: (kind, title, body) => `${kind}|${title}|${body}`}};
  ctx.window = ctx;
  vm.createContext(ctx);
  require("./helpers/load-time")(ctx);
  for (const file of ["web/js/fleet.js", "web/js/views-storage.js"]) vm.runInContext(fs.readFileSync(file, "utf8"), ctx);
  return ctx;
}

test("the Architecture page uses the same states and does not call a drawn graph unloaded", () => {
  const ctx = architecture();
  const drawn = ctx.archMissingCallout([{id: "office", name: "Office", state: "waiting", age_s: 95}]);
  assert.doesNotMatch(drawn, /could not be loaded/);
  assert.match(drawn, /may be out of date/);
  assert.match(drawn, /Office is slow to answer; its graph shown is from 1 min ago/);
  const gone = ctx.archMissingCallout([{id: "office", name: "Office", state: "not answering"}]);
  assert.match(gone, /Architecture is incomplete/);
  assert.match(gone, /Office is not answering, so it is missing here/);
  assert.equal(ctx.archMissingCallout([]), "");
});
