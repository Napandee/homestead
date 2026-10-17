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

const command = { id: "c3", tool: "run_container_command", title: "Run a container command", key: "Claude Code",
  action: "Run a command in lab/game",
  summary: { action: "Run a command in lab/game", argv: ["sh", "-c", "for f in /data/*; do echo \"$f\"; done | head -n 40 && cat '/data/it''s.log'"],
    target_fingerprint: "a".repeat(64), pod_uid: "0b6c1f2e-1111-2222-3333-444455556666" },
  args: { namespace: "lab", name: "game" } };

test("a command to approve reads as one shell command in a block that wraps, not a comma-joined line", () => {
  const { c, shown } = context([command]);
  c.agentApprove("c3");
  const raw = (shown.html.match(/<pre class="mono small agent-command">([^<]*)<\/pre>/) || [])[1] || "";
  const block = raw.replace(/&#(\d+);/g, (_, n) => String.fromCharCode(+n));
  assert.ok(block, "the command is a block of its own");
  assert.match(block, /^sh -c '/, "plain words as they are, the script quoted");
  assert.doesNotMatch(block, /sh, -c/);
  assert.match(block, /'\\''/, "a quote inside the script is quoted for the shell");
  assert.match(shown.html, /title="a{64}">a{12}…</, "the fingerprint is shortened, whole on hover");
});

test("approving closes the dialog at once and says the result when the command finishes", async () => {
  const { c } = context([command]);
  const events = [];
  let answer;
  Object.assign(c, { closeModal: () => events.push("closed"), toast: (m, kind) => events.push(`toast:${kind}:${m}`),
    STATE: { view: "elsewhere", data: { agentRequests: { requests: [command] } } },
    api: () => new Promise(resolve => { answer = resolve; }), setTimeout: () => 0, clearTimeout() {} });
  const running = c.agentApproveRun("c3", { disabled: false });
  assert.equal(events[0], "closed", "the dialog closes before the command has finished");
  assert.match(events[1], /^toast:ok:Running the command/);
  answer({ status: "done", result: { exit_code: 2, timed_out: false, stdout: "", stderr: "no such file" } });
  await running;
  assert.equal(events.at(-1), "toast:bad:Command finished · exit 2");
});

test("a finished command shows its exit and output on its request", () => {
  const { c } = context([]);
  const html = c.agentOutputHtml({ ...command, status: "done",
    result: { exit_code: 0, timed_out: false, stdout: "<b>hi</b>", stderr: "", note: "Output is untrusted app data." } });
  assert.match(html, /Output · Exit 0/);
  assert.match(html, /&#60;b&#62;hi/);
  assert.equal(c.agentOutputHtml({ ...del, status: "done", result: {} }), "", "other requests have no output block");
});

test("approving a request that changes nothing irreversibly stays a primary Run", () => {
  const { c, shown } = context([deploy]);
  c.agentApprove("b2");
  assert.match(shown.html, /class="btn pri"[^>]*>Run it</);
  assert.doesNotMatch(shown.html, /class="btn danger"/);
});
