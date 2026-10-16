"use strict";
// Approving an AI agent's request (#500): a delete or restore is a danger
// button named from the action, with one sentence and a short acknowledgement.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

function context(requests) {
  const shown = {};
  const c = { console, STATE: { data: { agentRequests: { requests } } }, document: { addEventListener() {} }, $: () => null, $$: () => [],
    esc: v => String(v ?? "").replace(/[&<>"']/g, ch => `&#${ch.charCodeAt(0)};`), icon: () => "",
    modal: (title, html) => Object.assign(shown, { title, html }) };
  c.window = c; vm.createContext(c); require("./helpers/load-ui")(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => c.esc(c.jsArg(s));
  vm.runInContext(fs.readFileSync("web/js/views-agents.js", "utf8"), c);
  return { c, shown };
}
const del = { id: "a1", tool: "delete_container", title: "Delete a container", key: "Claude Code", action: "Delete the container lab/old-wiki",
  summary: { action: "Delete the container lab/old-wiki", replicas: 0 }, args: { namespace: "lab", name: "old-wiki" } };
const deploy = { id: "b2", tool: "deploy_container", title: "Deploy a container", key: "Claude Code", action: "Deploy grafana/grafana:11 into lab",
  summary: { action: "Deploy grafana/grafana:11 into lab" }, args: { image: "grafana/grafana:11", namespace: "lab" } };

test("approving a delete is a danger button named from what it deletes", () => {
  const { c, shown } = context([del]);
  c.agentApprove("a1");
  assert.match(shown.html, /class="btn danger"[^>]*>Delete lab\/old-wiki</);
  assert.doesNotMatch(shown.html, /Approve and run/);
  assert.match(shown.html, /class="ui-lead"/);
  const ack = (shown.html.match(/<span>([^<]+)<\/span><\/label>/) || [])[1] || "";
  assert.ok(ack && ack.split(/\s+/).length <= 6, ack);
});

test("approving a request that changes nothing irreversibly stays a primary Run", () => {
  const { c, shown } = context([deploy]);
  c.agentApprove("b2");
  assert.match(shown.html, /class="btn pri"[^>]*>Run it</);
  assert.doesNotMatch(shown.html, /class="btn danger"/);
});
