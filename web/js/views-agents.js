/* AI agents (#417): the requests an agent made that a person has to approve,
   and the Settings card that turns the MCP server on and shows how to connect
   to it. The server files a request for every destructive tool an agent
   calls (server/homestead_agent_requests.py); this page is where it is read
   and answered. */

const AGENTS = { focused: "" };

/* "in 24 minutes", "in under a minute", "expired". */
function agentLeft(expires, now = Date.now() / 1000) {
  const left = Math.round((expires - now) / 60);
  if (expires - now <= 0) return "expired";
  return left < 1 ? "in under a minute" : `in ${left} minute${left === 1 ? "" : "s"}`;
}
window.agentLeft = agentLeft;

const agentWhen = at => at ? (typeof agoText === "function" ? agoText(at) : new Date(at * 1000).toLocaleString()) : "";
/* A request's field names as people write them; the rest lose their
   underscores. Ids, images and addresses are shown in mono. */
const AGENT_LABELS = { uid: "UID", ip: "IP", vip: "VIP", load_balancer_ip: "Load balancer IP", why_admin: "Admin because",
  pvc: "PVC", url: "URL", mac: "MAC" };
const AGENT_MONO = /(^|_)(uid|id|ip|vip|image|mac|url|pvc|digest|address)$/;
const agentLabel = key => AGENT_LABELS[key] || String(key).replace(/_/g, " ").replace(/^./, c => c.toUpperCase())
  .replace(/\b(ip|vip|uid|url|mac|pvc)\b/g, word => word.toUpperCase());

const AGENT_STATUS = { pending: ["waiting", "warn"], running: ["running", "info"], done: ["done", "ok"],
  denied: ["denied", "warn"], expired: ["expired", ""], failed: ["failed", "bad"] };
const agentStatusChip = status => UI.chip(...(AGENT_STATUS[status] || [status, ""]));

/* One value of a plan, as a person reads it. Lists of plain values are
   joined; anything nested is shown as compact text rather than guessed at. */
function agentValue(value, key = "") {
  if (value === null || value === undefined || value === "") return '<span class="dim">none</span>';
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "number" || typeof value === "string")
    return AGENT_MONO.test(key) ? `<span class="mono">${esc(String(value))}</span>` : esc(String(value));
  if (Array.isArray(value)) {
    if (!value.length) return '<span class="dim">none</span>';
    return value.every(v => v === null || typeof v !== "object") ? esc(value.join(", "))
      : value.map(v => `<div class="mono xs">${esc(JSON.stringify(v))}</div>`).join("");
  }
  return `<span class="mono xs">${esc(JSON.stringify(value))}</span>`;
}

/* The plan to review: everything in the summary except its headline. */
function agentPlanHtml(summary) {
  const rows = Object.entries(summary || {}).filter(([key]) => key !== "action")
    .map(([key, value]) => [agentLabel(key), agentValue(value, key)]);
  return rows.length ? UI.facts(rows) : "";
}

function agentCard(r, target) {
  const pending = r.status === "pending";
  const rawDetails = JSON.stringify({ summary: r.summary || {}, args: r.args || {} }, null, 2);
  const why = r.can_decide ? "" : `Needs the ${r.role} role`;
  return `<section class="card flat agent-req${target ? " target" : ""}" id="agent-req-${esc(r.id)}">
    <div class="between agent-req-head"><b>${esc(r.action || r.title)}</b>
      <span class="row">${r.escalated ? UI.chip("admin only", "warn") : ""}${agentStatusChip(r.status)}</span></div>
    ${UI.facts([
      ["Key", `${esc(r.key)}${r.owner ? `<div class="dim xs">made by ${esc(r.owner)}</div>` : ""}`],
      ["The agent says", r.reason ? esc(r.reason) : '<span class="dim">no reason given</span>'],
      ["Asked from", r.addr ? `<span class="mono">${esc(r.addr)}</span>` : '<span class="dim">unknown</span>'],
      pending ? ["Expires", esc(agentLeft(r.expires))] : ["Ended", esc(agentWhen(r.ended || r.created))],
      r.decided_by ? ["Decided by", esc(r.decided_by)] : null])}
    ${agentPlanHtml(r.summary)}
    ${!pending && r.detail ? `<p class="small agent-req-detail">${esc(r.detail)}</p>` : ""}
    ${UI.more("Raw details", `<pre class="mono small agent-raw">${esc(rawDetails)}</pre>`, false, `agent-raw-${r.id}`)}
    ${pending ? `<div class="agent-req-actions">${why ? `<span class="dim small">${esc(why)}</span>` : ""}${actionBar([
      { label: "Deny", run: `agentDeny(${jsq(r.id)})`, disabled: !r.can_decide, tip: why },
      { label: "Approve", run: `agentApprove(${jsq(r.id)})`, pri: true, disabled: !r.can_decide, tip: why }], { shown: 2 })}</div>` : ""}
  </section>`;
}

async function viewAgents() {
  let found;
  try { found = await api("/api/agents/requests"); }
  catch (e) { paint(UI.error(e, { retry: "viewAgents()" })); return; }
  STATE.data.agentRequests = found;
  const rows = found.requests || [];
  const wanted = new URLSearchParams(location.search).get("request") || "";
  const pending = rows.filter(r => r.status === "pending");
  const ended = rows.filter(r => r.status !== "pending");
  const named = ended.find(r => r.id === wanted);
  paint(`${UI.pageHeader("AI agent requests", pending.length ? `${pending.length} waiting for a person` : "Nothing is waiting",
      `<a class="btn" href="${esc(HomesteadRouter.urlFor("settings", { tab: "access" }))}" onclick="event.preventDefault();settingsTab('access');go('settings')">Agent settings</a>`)}
    ${named ? UI.section("This request", agentCard(named, true)) : ""}
    ${UI.section("Waiting for approval", pending.length ? `<div class="agent-list">${pending.map(r => agentCard(r, r.id === wanted)).join("")}</div>`
      : UI.table([], [], { empty: "No AI agent is waiting for an answer. Requests an agent makes for a destructive action appear here and in the bell." }))}
    ${UI.section("Recent requests", UI.table(
      [{ label: "Request" }, { label: "Result" }, { label: "Decided" }, { label: "Detail" }],
      ended.filter(r => r !== named).map(r => [
        `<b>${esc(r.action || r.title)}</b><div class="dim xs">${esc(r.key)}${r.owner ? ` · made by ${esc(r.owner)}` : ""}</div>`,
        agentStatusChip(r.status),
        `${r.decided_by ? esc(r.decided_by) + " · " : ""}${esc(agentWhen(r.ended || r.created))}`,
        esc(r.detail || "")]),
      { empty: "No request has been answered in the last week." }))}`);
  // Opened from the bell: bring the request into view once, not on every refresh.
  if (wanted && AGENTS.focused !== wanted) {
    const card = document.getElementById("agent-req-" + wanted);
    if (card) {
      AGENTS.focused = wanted;
      card.scrollIntoView({ block: "center", behavior: "smooth" });
      card.classList.add("flash-find");
      setTimeout(() => card.classList.remove("flash-find"), 2600);
    }
  }
}
window.viewAgents = viewAgents;

const agentPost = (path, id) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id }) });

/* Approving runs the plan, as the person approving, so it asks first. */
window.agentApprove = id => {
  const r = (STATE.data.agentRequests?.requests || []).find(x => x.id === id);
  if (!r) return;
  modal("Approve this request", UI.callout("warn", r.action || r.title, `${esc(r.key)} asked for this. Approving runs it now, as you.`)
    + agentPlanHtml(r.summary)
    + UI.ack("agentOk", "Run it now, as the plan above describes", { onchange: "$('#agentGo').disabled=!this.checked" })
    + UI.actions(UI.cancel() + UI.button("Approve and run", `agentApproveRun(${jsArg(id)},this)`, { kind: "pri", id: "agentGo", disabled: true })));
};

window.agentApproveRun = async (id, button) => {
  if (button) button.disabled = true;
  try {
    const r = await agentPost("/api/agents/requests/approve", id);
    closeModal();
    toast(r.status === "failed" ? (r.detail || "It could not run") : (r.detail || "Approved"), r.status === "failed" ? "bad" : "ok");
  } catch (e) { toast(e.message, "bad"); if (button) button.disabled = false; return; }
  viewAgents();
};

window.agentDeny = async id => {
  try { await agentPost("/api/agents/requests/deny", id); toast("Denied; the agent is told", "ok"); }
  catch (e) { toast(e.message, "bad"); }
  viewAgents();
};

/* ---------------- Settings › Users and access ---------------- */
const AGENT_OUTCOME = { ok: "ok", approved: "ok", pending: "info", denied: "warn", refused: "warn", error: "bad", expired: "" };

window.mcpCardPaint = async () => {
  const card = document.getElementById("mcpCard");
  if (!card || !can("admin")) return;
  const [requests, audit] = await Promise.all([
    api("/api/agents/requests").catch(() => ({ pending: 0 })),
    api("/api/agents/audit?limit=20").catch(e => ({ calls: [], error: e.message }))]);
  const on = !!STATE.data.appSettings?.mcp?.enabled;
  const url = location.origin + "/mcp";
  window.__mcpText = {
    url,
    code: `claude mcp add --transport http --scope user homestead ${url} --header "Authorization: Bearer <key>"`,
    desktop: JSON.stringify({ mcpServers: { homestead: { command: "npx", args: ["mcp-remote", url, "--header", "Authorization: Bearer ${HOMESTEAD_KEY}"],
      env: { HOMESTEAD_KEY: "hsk_..." } } } }, null, 2) };
  const copy = (name, label = "Copy") => `<button type="button" class="btn sm" onclick="copyText(window.__mcpText.${name}).then(ok => toast(ok ? 'Copied' : 'Select it and copy', ok ? 'ok' : 'bad'))">${label}</button>`;
  const waiting = requests.pending || 0;
  card.innerHTML = `${UI.moduleHeader("AI agents (MCP)", "Let an AI assistant such as Claude manage this cluster with an API key, through the Model Context Protocol")}
    <div class="srows">${settingRow("Allow AI agents to connect", "Off, every request to the endpoint is refused. Each agent still needs an API key marked for AI agents.",
      `<label class="toggle"><input type="checkbox" id="mcp_on" ${on ? "checked" : ""} onchange="mcpSwitch(this)"><span></span></label>`)}</div>
    ${location.protocol === "http:" ? UI.callout("warn", "This page is not served over HTTPS", STATE.data.apiKeys?.you?.node_address
      ? "An agent's key would travel unencrypted on the network. Set up HTTPS first. Limiting keys to a client's address does not work on this cluster: Homestead sees a node's address for every request (see API keys)."
      : "An agent's key would travel unencrypted on the network. Set up HTTPS first, and limit each key to the address of the client that uses it.") : ""}
    ${UI.field("Endpoint", `<div class="row" style="gap:8px;flex-wrap:nowrap"><input class="mono" readonly value="${esc(url)}" onclick="this.select()">${copy("url")}</div>`,
      { help: "Make a key under API keys with Allow AI agents (MCP) ticked, then give it to the client." })}
    ${UI.more("Connect Claude Code", `<pre class="mono small agent-raw">${esc(window.__mcpText.code)}</pre><div class="row">${copy("code")}</div>`, false, "mcp-claude-code")}
    ${UI.more("Connect Claude Desktop", `<p class="small">Add this to claude_desktop_config.json, with the key in place of hsk_...</p><pre class="mono small agent-raw">${esc(window.__mcpText.desktop)}</pre><div class="row">${copy("desktop")}</div>`, false, "mcp-claude-desktop")}
    <p class="small">Reading and routine changes run at once. Anything that deletes or restores data waits for a person to approve it on
      <a href="${esc(HomesteadRouter.urlFor("agents"))}" onclick="event.preventDefault();go('agents')">AI agent requests</a>${waiting ? ` ${UI.chip(waiting + " waiting", "warn")}` : ""}.</p>
    ${UI.section("Recent calls", audit.error ? `<div class="dim small">${esc(audit.error)}</div>` : UI.table(
      [{ label: "When" }, { label: "Key" }, { label: "Tool or action" }, { label: "Outcome" }],
      (audit.calls || []).map(c => [esc(agentWhen(c.at)), esc(c.name || c.key), `<span class="mono xs">${esc(c.action)}</span>${c.via === "api" ? ' <span class="dim xs">API</span>' : ""}`,
        `<span title="${esc(c.detail || "")}">${UI.chip(c.outcome, AGENT_OUTCOME[c.outcome] || "")}</span>`]),
      { empty: "No agent has called yet." }))}`;
};

window.mcpSwitch = async box => {
  const enabled = box.checked;
  box.disabled = true;
  try {
    const current = { ...(await loadHealthSettings()) };
    delete current.info;
    await api("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...current, thresholds: current.thresholds || HEALTH_DEFAULTS.thresholds, mcp: { enabled } }) });
    STATE.data.appSettings = null;
    await loadHealthSettings(true);
    toast(enabled ? "AI agents may connect" : "AI agents are refused", "ok");
  } catch (e) { box.checked = !enabled; toast(e.message, "bad"); }
  finally { box.disabled = false; }
};
