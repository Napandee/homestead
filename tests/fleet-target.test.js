"use strict";

/* In the view of all clusters, an action taken on another cluster's row - or
   in a dialog or menu opened from it - goes to that cluster; anything done on
   the page itself afterwards goes to this one (#598). */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

// An element that knows which of the given selectors it sits inside.
function element(inside = [], extra = {}) {
  return { closest: selector => selector.split(",").map(s => s.trim()).some(s => inside.includes(s)) ? { dataset: extra.dataset || {} } : null,
    ...extra };
}

function setup() {
  let onClick = null;
  const modal = { classList: { contains: name => name === "hidden" ? !ctx.dialogOpen : false } };
  const ctx = { console, STATE: { view: "workloads", data: {} }, dialogOpen: false,
    document: { addEventListener(kind, fn) { if (kind === "click") onClick = fn; } },
    $: id => (id === "#modal" ? modal : null), toast() {}, esc: String, jsq: String, jsArg: String,
    localStorage: { getItem: () => null, setItem() {} }, location: { pathname: "/hosts", search: "", href: "https://h/hosts" } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/fleet.js", "utf8"), ctx);
  ctx.FLEET.view = { self: "home", linked: true, members: [] };
  const click = target => onClick({ target });
  const rowOf = cluster => element(["[data-cluster]", "#app", "#views"], { dataset: { cluster } });
  const save = () => ctx.fleetRoute("/api/settings", { method: "POST", headers: {} }).opts.headers?.["X-Homestead-Cluster"] || "";
  return { ctx, click, rowOf, save };
}

test("a row's cluster carries into the dialog opened from it", () => {
  const t = setup();
  t.click(t.rowOf("branch"));
  t.ctx.dialogOpen = true;
  t.click(element(["#modal", "#app"]));
  assert.equal(t.save(), "branch");
});

test("a confirmation, a row's menu or a suggestion list keeps the row's cluster", () => {
  const t = setup();
  t.click(t.rowOf("branch"));
  t.click(element([".askdlg"]));
  assert.equal(t.save(), "branch");
  t.click(element([".suggest-pop"]));
  assert.equal(t.save(), "branch");
  // The "…" menu is moved out of its row; it still belongs to that row.
  t.click(t.rowOf("home"));
  t.click(element([".actionmenu-portal"], { closest(selector) {
    if (selector.includes(".actionmenu-portal")) return { owner: t.rowOf("branch") };
    return null;
  } }));
  assert.equal(t.save(), "branch");
});

test("after a row on another cluster, the phone's bottom bar, search or a page button act here", () => {
  for (const place of ["#bottombar", "#searchwrap", "#views", "#crumb"]) {
    const t = setup();
    t.click(t.rowOf("branch"));
    t.click(element([place, "#app"]));
    assert.equal(t.save(), "", `a click in ${place} acts on this cluster`);
  }
});

test("moving to another page without a click (Back, a link) acts here", () => {
  const t = setup();
  t.click(t.rowOf("branch"));
  t.ctx.STATE.view = "settings";
  assert.equal(t.save(), "");
});

test("the sidebar and the cluster switch still act here, as before", () => {
  const t = setup();
  t.click(t.rowOf("branch"));
  t.click(element(["#nav", "#app"]));
  assert.equal(t.save(), "");
});

test("a page opened from another cluster's row - a host's own page - acts on that cluster", () => {
  const t = setup();
  t.ctx.STATE.view = "nodes";
  t.click(t.rowOf("branch"));                       // a host card on cluster B opens that host's page
  t.ctx.location.href = "https://h/hosts?node=b1";  // same view, its own address
  t.click(element(["#views", "#app"]));             // Host actions, Terminal, a section tab
  assert.equal(t.save(), "branch");
  t.click(element(["#crumb", "#app"]));             // back to the list by the breadcrumb
  assert.equal(t.save(), "");
});

test("back on the list, a page button acts here again", () => {
  const t = setup();
  t.ctx.STATE.view = "nodes";
  t.click(t.rowOf("branch"));
  t.click(element(["#views", "#app"]));             // still the list: New, Balance hosts
  assert.equal(t.save(), "");
});

test("the job tray acts here, not on the last row's cluster", () => {
  const t = setup();
  t.click(t.rowOf("branch"));
  t.click(element(["#jobTray", "#app"]));
  assert.equal(t.save(), "");
});
