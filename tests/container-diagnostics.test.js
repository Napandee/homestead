const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

function context(admin = true) {
  const shown = {}, elements = {}, calls = [];
  const c = { console, URLSearchParams, can: () => admin, document: {}, $: id => elements[id],
    esc: v => String(v ?? "").replace(/[&<>"']/g, ch => `&#${ch.charCodeAt(0)};`), icon: () => "",
    modal: (title, html) => Object.assign(shown, { title, html }), toast() {}, closeModal() {},
    api: async (path, init) => { calls.push({ path, init }); return {}; } };
  c.window = c; vm.createContext(c); require("./helpers/load-ui")(c);
  c.jsArg = s => JSON.stringify(String(s ?? ""));
  vm.runInContext(fs.readFileSync("web/js/container-diagnostics.js", "utf8"), c);
  return { c, shown, elements, calls };
}
const report = { ns: "apps", name: "game", pod: "game-1", container: "server", pods: [{ name: "game-1", node: "node-1",
  containers: [{ name: "server", kind: "app", restarts: 5, state: { waiting: { reason: "CrashLoopBackOff" } },
    last_state: { terminated: { reason: "Error", exitCode: 127 } } }] }],
  logs: { previous: { text: "<script>alert(1)</script>" }, current: { error: "Waiting to start" } } };

test("diagnostics show the previous exit and escape app-controlled output", () => {
  const { c } = context();
  const html = c.diagnosticMarkup(report);
  assert.match(html, /code 127/);
  assert.match(html, /CrashLoopBackOff/);
  assert.match(html, /Previous run/);
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /Run a command/);
});

test("Diagnostics has an icon of its own, not Search logs' magnifying glass", () => {
  // A row shows it as an icon only, beside Logs, with Search logs in the same menu.
  const source = fs.readFileSync("web/js/views-workloads.js", "utf8");
  const iconOf = label => source.match(new RegExp(`item\\("${label}", [^\\n]*?, "([a-z-]+)"`))?.[1];
  assert.ok(iconOf("Diagnostics"), "the Diagnostics action is found");
  assert.notEqual(iconOf("Diagnostics"), iconOf("Search logs"));
  assert.notEqual(iconOf("Diagnostics"), iconOf("Logs"));
});

test("viewers see evidence without a command runner", () => {
  const { c } = context(false);
  assert.doesNotMatch(c.diagnosticMarkup(report), /diagnosticCommand/);
});

test("review serializes exact shell arguments and never executes the command", async () => {
  const { c, elements, calls, shown } = context();
  c.testReport = report; vm.runInContext("containerDiagnostics = testReport", c);
  elements["#diagnosticCommand"] = { value: "printf '%s' 'a;b'" };
  elements["#diagnosticTimeout"] = { value: "10" };
  elements["#containerDiagnosticBody"] = {};
  c.api = async (path, init) => { calls.push({ path, init }); return { settings: JSON.parse(init.body), review_token: "review", summary: { limit: "Bounded" } }; };
  await c.diagnosticReview({ disabled: false });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, "/api/container/command/preview");
  assert.deepEqual(JSON.parse(calls[0].init.body).argv, ["sh", "-c", "printf '%s' 'a;b'"]);
  assert.match(shown.html, /Run this exact command/);
  assert.match(shown.html, /id="diagnosticRun"[^>]*disabled/);
});

test("a stale diagnostics fetch does not replace a newer selection", async () => {
  const { c, elements } = context();
  elements["#containerDiagnosticBody"] = { innerHTML: "" };
  let first;
  c.api = path => path.includes("name=first") ? new Promise(r => { first = r; }) : Promise.resolve(report);
  const pending = c.wlDiagnostics("apps", "first");
  await c.wlDiagnostics("apps", "game");
  const latest = elements["#containerDiagnosticBody"].innerHTML;
  first({ message: "stale", pods: [] }); await pending;
  assert.equal(elements["#containerDiagnosticBody"].innerHTML, latest);
});
