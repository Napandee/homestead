const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const vm = require("vm");

const src = fs.readFileSync("web/js/views-settings.js", "utf8");
const start = src.indexOf("const DAYS = ["), end = src.indexOf("window.updateWindowEdit");
const ctx = { Date, String, Math, Number, Set };
vm.createContext(ctx);
vm.runInContext(src.slice(start, end) + ";this.updateWindowText = updateWindowText; this.updateWindowDays = updateWindowDays;", ctx);

test("the days read at a glance", () => {
  assert.equal(ctx.updateWindowDays([0, 1, 2, 3, 4, 5, 6]), "Every day");
  assert.equal(ctx.updateWindowDays([0, 1, 2, 3, 4]), "Weekdays");
  assert.equal(ctx.updateWindowDays([5, 6]), "Weekends");
  assert.equal(ctx.updateWindowDays([1, 2, 3, 4, 5, 6]), "Tue–Sun");
  assert.equal(ctx.updateWindowDays([0, 2, 4]), "Mon, Wed, Fri");
  assert.equal(ctx.updateWindowDays([]), "No days");
});

test("the window says when it ends, in UTC", () => {
  const text = ctx.updateWindowText({ days: [0, 1, 2, 3, 4], start: "02:00", duration_minutes: 120 });
  assert.match(text, /^Weekdays · 02:00–04:00 UTC/);
  assert.match(ctx.updateWindowText({ days: [6], start: "23:30", duration_minutes: 60 }), /23:30–00:30 UTC/, "past midnight");
});

/* The window's dialog against a stubbed page: the Policy select holds a
   change still waiting on the save bar. */
function windowPage(stored) {
  const fields = {
    "#set_update_policy": { value: "notify_only" },
    "#set_notify_available": { checked: true },
    "#set_notify_failures": { checked: true },
    "#set_update_start": { value: "03:00" },
    "#set_update_duration": { value: "90" },
    "#set_update_window": { textContent: "Every day · 02:00–04:00 UTC" },
  };
  const page = { posted: [], rendered: 0, fields };
  const appSave = src.slice(src.indexOf("async function settingsAppSave"), src.indexOf("window.settingsSave"));
  // Every save goes through settingsWrite (#601); a missing marker fails here, not silently.
  const writeAt = src.indexOf("window.settingsWrite"), writeEnd = src.indexOf("window.permissionsRecheck");
  assert.ok(writeAt >= 0 && writeEnd > writeAt, "settingsWrite is found in views-settings.js");
  const write = src.slice(writeAt, writeEnd);
  const dialog = src.slice(start, src.indexOf("window.catalogEdit"));
  const win = {
    Date, String, Math, Number, Set, JSON, Error, Object, Array,
    STATE: { data: { appSettings: stored }, settingsDirty: new Set(["app:set_update_policy"]) },
    HEALTH_DEFAULTS: { thresholds: {} }, HEALTH: {},
    $: selector => fields[selector] || null,
    $$: selector => selector === ".set_update_day:checked" ? [{ value: "0" }, { value: "2" }] : [],
    // A save reads the settings as they are now first (#601): here, the stored ones.
    api: async (path, init) => {
      if (!init || !init.method) return JSON.parse(JSON.stringify(stored));
      page.posted.push(JSON.parse(init.body)); return JSON.parse(init.body);
    },
    loadHealthSettings: async () => { win.STATE.data.appSettings = stored; },
    loadImageUpdates: () => {}, settingsTab: () => {}, closeModal: () => {}, toast: () => {},
    resetPaint: () => {}, viewSettings: () => { page.rendered++; }, esc: String,
  };
  win.window = win;
  vm.createContext(win);
  vm.runInContext(write + appSave + dialog, win);
  page.win = win;
  return page;
}

test("saving the update window sends only the window, and a pending policy change stays pending", async () => {
  const stored = { thresholds: { cpu: { warning: 70, critical: 88 } }, site_name: "Main site", catalog_url: "",
    placement: { rebalance: "suggest" }, mcp: { enabled: true },
    updates: { policy: "approval_required", notify_available: true, notify_failures: false,
      maintenance: { start: "02:00", duration_minutes: 120, days: [0, 1, 2, 3, 4, 5, 6] } },
    info: { version: "2.8.326-dev.1" } };
  const page = windowPage(stored);
  await page.win.updateWindowSave();
  assert.equal(page.posted.length, 1);
  const { info, ...rest } = stored;
  assert.deepStrictEqual(page.posted[0], { ...rest, updates: { ...stored.updates,
    maintenance: { start: "03:00", duration_minutes: 90, days: [0, 2] } } }, "the stored settings, with only the window changed");
  assert.deepStrictEqual([...page.win.STATE.settingsDirty], ["app:set_update_policy"], "the policy change is still unsaved");
  assert.equal(page.rendered, 0, "the section is not re-rendered, which would put the Policy back");
  assert.equal(page.fields["#set_update_policy"].value, "notify_only");
  assert.match(page.fields["#set_update_window"].textContent, /^Mon, Wed · 03:00–04:30 UTC/, "the window's row shows the saved window");
  assert.equal(page.win.STATE.data.appSettings.updates.maintenance.start, "03:00");
  assert.equal(page.win.STATE.data.appSettings.info.version, "2.8.326-dev.1");
});
