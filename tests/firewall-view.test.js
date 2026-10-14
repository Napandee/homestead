"use strict";

/* Networking > Firewall: each direction says which it is and what it means,
   in the editor's words, and the summary warns when nothing may enforce it. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

function view(provider, policies) {
  const out = {};
  const ctx = { console, STATE: { data: {} }, document: { addEventListener() {} }, $: () => null, $$: () => [],
    esc: v => String(v ?? "").replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`), icon: () => "", tip: () => "",
    api: async () => ({ provider, policies }), networkPage: (key, html) => { out.html = html; } };
  ctx.window = ctx; vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => ctx.esc(ctx.jsArg(s));
  vm.runInContext(fs.readFileSync("web/js/views-firewall.js", "utf8"), ctx);
  return { ctx, out };
}
const policy = spec => ({ namespace: "lab", name: "homestead-fw-frigate", managed: true, spec,
  config: { target: { name: "frigate", kind: "Deployment" } } });

test("each direction reads in the editor's words", () => {
  const { ctx } = view();
  const direction = vm.runInContext("firewallDirection", ctx);
  assert.equal(direction({ policyTypes: ["Ingress"], ingress: [{}] }, "Egress"), "unrestricted");
  assert.equal(direction({ policyTypes: ["Ingress"], ingress: [] }, "Ingress"), "nothing allowed");
  assert.equal(direction({ policyTypes: ["Ingress"], ingress: [{}] }, "Ingress"), "1 allow rule");
  assert.equal(direction({ policyTypes: ["Ingress", "Egress"], egress: [{}, {}] }, "Egress"), "2 allow rules");
});

test("the policy list names the direction of each line", async () => {
  const { ctx, out } = view({ name: "K3s", found: true, detail: "" }, [policy({ policyTypes: ["Ingress"], ingress: [{}] })]);
  await ctx.viewFirewall();
  assert.match(out.html, /In: 1 allow rule/);
  assert.match(out.html, /Out: unrestricted/);
});

test("the summary says a controller was found, or warns that none was", async () => {
  let { ctx, out } = view({ name: "Calico", found: true, detail: "" }, []);
  await ctx.viewFirewall();
  assert.match(out.html, /Calico found · enforcement not tested/);
  assert.doesNotMatch(out.html, /No policy controller found/);
  ({ ctx, out } = view({ name: "Unverified", found: false, detail: "" }, []));
  await ctx.viewFirewall();
  assert.match(out.html, /ui-callout warn[\s\S]*No policy controller found: these policies may not filter traffic/);
  assert.doesNotMatch(out.html, /Unverified · enforcement/);
});
