/* Log alerts: a saved log search that raises an alert (homestead_logalerts.py).
   "More than 10 lines matching this search a minute from app X." The leader
   counts once a minute; an alert carries the rule's name, its apps and a
   count, never a line and never the query: lines and searches can hold
   secrets, and alerts reach lock screens. The lines stay behind the
   signed-in search. Saving and deleting need operator; anyone signed in can
   see the rules and try one on the last hour, as anyone can search. */
const LOGALERT = { draft: null, rules: [] };
const LOGALERT_WINDOWS = [[1, "a minute"], [5, "5 minutes"], [15, "15 minutes"]];
const logAlertSpan = window => (LOGALERT_WINDOWS.find(w => w[0] === window) || LOGALERT_WINDOWS[0])[1];

/* The name an alert starts with: the app and a plain label. Never the query,
   which can hold the very secret it looks for. */
function logAlertDefaultName(app) {
  const name = String(app || "").split("/")[1] || "";
  return name ? `${name} log alert` : "Log alert";
}

/* Per-minute counts as bars, summed over the rule's window, with the
   threshold drawn across. A minute that was not read (null) is a short grey
   tick, never a zero. */
function logAlertBars(counts, threshold, window = 1, { h = 64 } = {}) {
  const vals = (counts || []).map(c => c[1]);
  if (!vals.length) return `<div class="dim xs">No counts yet: the first arrives within a minute of saving.</div>`;
  const sums = vals.map((v, i) => v === null ? null : vals.slice(Math.max(0, i - window + 1), i + 1).reduce((a, b) => a + (b || 0), 0));
  const top = Math.max(threshold * 1.4, ...sums.map(v => v || 0), 1), w = 300, bw = w / Math.max(60, vals.length);
  const y = v => h - (v / top) * (h - 4);
  const over = sums.filter(v => v !== null && v > threshold).length, unread = sums.filter(v => v === null).length;
  const bars = sums.map((v, i) => v === null
    ? `<rect x="${(i * bw + 0.5).toFixed(1)}" y="${h - 3}" width="${Math.max(1, bw - 1).toFixed(1)}" height="3" class="la-unread"/>`
    : `<rect x="${(i * bw + 0.5).toFixed(1)}" y="${y(v).toFixed(1)}" width="${Math.max(1, bw - 1).toFixed(1)}" height="${(h - y(v)).toFixed(1)}" class="${v > threshold ? "la-over" : "la-under"}"/>`).join("");
  const label = `Matching lines per minute over the last hour; ${over} minute${over === 1 ? "" : "s"} over ${threshold}${unread ? `; ${unread} minute${unread === 1 ? "" : "s"} not read` : ""}`;
  return `<svg class="la-bars" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="${esc(label)}">
    ${bars}<line x1="0" x2="${w}" y1="${y(threshold).toFixed(1)}" y2="${y(threshold).toFixed(1)}" class="la-limit"/></svg>
    <div class="la-axis dim xs"><span>1 hour ago</span><span>more than ${threshold} ${window === 1 ? "a minute" : `in ${window} minutes`}</span><span>now</span></div>`;
}

/* What stopped the last count, if anything: a rule not counted is never shown as quiet. */
function logAlertStatus(status) {
  const state = status?.state || "";
  if (!["unread", "skipped", "slow", "failed"].includes(state)) return "";
  return `<div class="la-status small">${UI.chip("Could not count", "warn")} ${esc(status.detail || "")}</div>`;
}

function logAlertRow(r, raised) {
  const known = (r.counts || []).filter(c => c[1] !== null), recent = known.slice(-r.window).reduce((a, c) => a + c[1], 0);
  const app = r.apps[0] || "/", [ns, name] = app.split("/");
  const change = can("operator")
    ? UI.button("Edit", `logAlertEdit(${jsArg(r.id)})`, { icon: "edit" }) + UI.button("Delete", `logAlertDelete(${jsArg(r.id)})`, { icon: "trash" })
    : "";
  const chip = raised ? UI.chip("Raised", "bad") : ["unread", "skipped", "slow", "failed"].includes(r.status?.state) ? "" : UI.chip("Watching", "ok");
  return `<div class="la-rule${raised ? " la-raised" : ""}">
    <div class="la-rule-head"><b>${esc(r.name)}</b>${chip}</div>
    <div class="dim small">${esc(r.apps.map(a => a.split("/")[1]).join(", "))} · more than ${Number(r.threshold)} lines matching <code>${esc(r.query)}</code>${r.regex ? " (expression)" : ""} in ${esc(logAlertSpan(r.window))}${known.length ? ` · last ${esc(logAlertSpan(r.window))}: ${recent}` : ""}</div>
    ${logAlertStatus(r.status)}
    ${logAlertBars(r.counts, r.threshold, r.window, { h: 44 })}
    <div class="la-rule-actions">${UI.button("Search these lines", `closeModal();logSearch(${jsArg(ns)},${jsArg(name)})`, { icon: "search" })}${change}</div></div>`;
}

/* From a search's form: the same query and options, the app chosen there. */
window.logAlertFromSearch = () => {
  const app = $("#ls_app")?.value || LOGSEARCH.app;
  LOGALERT.draft = { name: logAlertDefaultName(app), apps: app ? [app] : [], query: $("#ls_q")?.value || LOGSEARCH.query,
    regex: !!$("#ls_regex")?.checked, case: !!$("#ls_case")?.checked, threshold: 10, window: 1, severity: "degraded" };
  logAlertEdit();
};

window.logAlertEdit = (id = "") => {
  const rule = id ? LOGALERT.rules.find(r => r.id === id) : LOGALERT.draft;
  if (!rule) return;
  LOGALERT.draft = { ...rule };
  const apps = (STATE.data.wl || []).filter(w => !w.platform).map(w => `${w.ns}/${w.name}`).sort((a, b) => a.split("/")[1].localeCompare(b.split("/")[1]));
  const several = rule.apps.length > 1;
  const appField = several
    ? UI.field("Apps", `<div class="small">${esc(rule.apps.map(a => a.split("/")[1]).join(", "))}</div>`)
    : UI.field("App", `<select id="la_app" onchange="logAlertPreview()">${[`<option value="">Choose an app…</option>`, ...apps.map(a =>
        `<option value="${esc(a)}"${rule.apps[0] === a ? " selected" : ""}>${esc(a.split("/")[1])} · ${esc(a.split("/")[0])}</option>`)].join("")}</select>`,
        { help: "A log alert watches one app; never every app." });
  modal(id ? `Edit log alert · ${rule.name}` : "Alert on this search", `<div class="ui-stack">
    ${UI.lead("Homestead counts the lines that match once a minute and raises an alert when there are too many. The alert says how many, never what the lines say or what you searched for: both can hold passwords and tokens, and alerts reach lock screens.")}
    ${UI.fields(
      UI.field("Name", `<input id="la_name" maxlength="60" value="${esc(rule.name || logAlertDefaultName(rule.apps[0]))}">`, { help: "Shown in the alert. It may not repeat the search text." }),
      appField,
      UI.field("Lines matching", `<input id="la_q" maxlength="200" class="mono" value="${esc(rule.query)}">`,
        { help: `${rule.regex ? "Regular expression" : "Plain text"}, ${rule.case ? "matching case" : "any case"}, as in the search.` }),
      UI.field("More than", `<div class="la-thresh"><input id="la_n" type="number" min="1" inputmode="numeric" aria-label="Number of lines" value="${Number(rule.threshold) || 10}">
        <select id="la_w" aria-label="Counted over">${LOGALERT_WINDOWS.map(([v, label]) => `<option value="${v}"${v === rule.window ? " selected" : ""}>lines in ${esc(label)}</option>`).join("")}</select></div>`),
      UI.field("Raise it as", `<select id="la_sev">${[["info", "Information"], ["degraded", "Warning"], ["critical", "Critical"]].map(([v, label]) =>
        `<option value="${v}"${rule.severity === v ? " selected" : ""}>${label}</option>`).join("")}</select>`))}
    <div class="la-preview" id="la_preview"></div>
    ${UI.actions(UI.cancel() + (can("operator") ? UI.button("Save alert", "logAlertSave()", { kind: "pri", icon: "bell", id: "la_save" }) : ""),
      UI.button("Try on the last hour", "logAlertPreview()", { icon: "search" }))}</div>`, true);
  logAlertPreview();
};

function logAlertForm() {
  const app = $("#la_app")?.value;
  return { ...LOGALERT.draft, name: $("#la_name").value.trim(), apps: $("#la_app") ? (app ? [app] : []) : LOGALERT.draft.apps,
    query: $("#la_q").value, threshold: Number($("#la_n").value), window: Number($("#la_w").value), severity: $("#la_sev").value };
}

window.logAlertPreview = async () => {
  const out = $("#la_preview"), body = logAlertForm();
  if (!out) return;
  if (!body.apps.length) { out.innerHTML = `<div class="dim small">Choose an app to see how often this would have been raised.</div>`; return; }
  out.innerHTML = `<div class="empty small"><span class="spin2"></span> Trying it on the last hour…</div>`;
  const { id, counts, status, ...rule } = body;
  try {
    const d = await api("/api/logalerts/preview", { method: "POST", body: JSON.stringify(rule) });
    const total = d.counts.reduce((a, c) => a + c[1], 0);
    const gaps = [...(d.unread || []).map(a => `Could not read ${a}: its lines are not counted here.`),
      ...(d.capped || []).map(a => `${a} wrote more than Homestead reads at once: at least this many.`)];
    out.innerHTML = `<div class="la-preview-head"><b>Over the last hour</b>
      <span class="dim small">${total} matching line${total === 1 ? "" : "s"} · ${d.over_minutes ? `<b class="la-over-t">over the threshold in ${d.over_minutes} of 60 minutes</b>` : "never over the threshold"}</span></div>
      ${logAlertBars(d.counts, body.threshold, body.window)}
      ${gaps.length ? UI.callout("warn", "Part of the hour was not read", gaps.map(g => `<div>${esc(g)}</div>`).join("")) : ""}
      ${d.over_minutes > 20 ? UI.callout("warn", "This would be raised most of the time", "Raise the threshold, or count over 5 minutes, so the alert means something when it comes.") : ""}`;
  } catch (e) { out.innerHTML = UI.callout("bad", "Could not try it", esc(e.message)); }
};

window.logAlertSave = async () => {
  const { counts, status, ...body } = logAlertForm();
  const button = $("#la_save");
  if (button) button.disabled = true;
  try {
    await api("/api/logalerts", { method: "POST", body: JSON.stringify(body) });
    toast(`Saved: ${body.name}`);
    logAlerts();
  } catch (e) {
    $("#la_preview")?.insertAdjacentHTML("afterbegin", UI.callout("bad", "Not saved", esc(e.message)));
    if (button) button.disabled = false;
  }
};

window.logAlertDelete = async id => {
  const rule = LOGALERT.rules.find(r => r.id === id);
  if (!rule || !await ask(`Delete the log alert "${rule.name}"? It stops counting, and an alert it raised clears.`, { title: "Delete log alert", ok: "Delete", danger: true })) return;
  try {
    await api("/api/logalerts/delete", { method: "POST", body: JSON.stringify({ id }) });
    toast(`Deleted: ${rule.name}`);
  } catch (e) { toast(e.message, "bad"); }
  logAlerts();
};

/* Every log alert: what it counts, its last hour, and whether it is raised now. */
window.logAlerts = async () => {
  modal("Log alerts", `<div id="la_list"><div class="empty small"><span class="spin2"></span> Loading…</div></div>`, true);
  let d;
  try { d = await api("/api/logalerts"); } catch (e) { $("#la_list").innerHTML = UI.error(e, { dialog: true, retry: "logAlerts()" }); return; }
  LOGALERT.rules = d.rules || [];
  let raised = new Set(), bell = "";
  try {
    raised = new Set(((await api("/api/alerts")).active || []).filter(a => a.key?.startsWith("logs:")).map(a => a.key.slice(5)));
  } catch (e) { bell = UI.callout("warn", "Could not read which are raised now", esc(e.message)); }
  const list = $("#la_list");
  if (!list) return;
  list.innerHTML = `<div class="ui-stack">
    ${UI.lead(`Saved log searches that raise an alert. The leader counts each once a minute, reading only what its app wrote since the last count; the alert carries the count, never the lines. ${LOGALERT.rules.length} of ${Number(d.max_rules) || 20}.`)}
    ${bell}
    ${LOGALERT.rules.length ? `<div class="la-rules">${LOGALERT.rules.map(r => logAlertRow(r, raised.has(r.id))).join("")}</div>`
      : UI.empty(can("operator") ? "No log alerts yet. Search the logs, then choose Alert on this search." : "No log alerts yet.")}
    ${UI.actions(UI.cancel("Close"), UI.button("Search logs", "logSearch()", { icon: "search" }))}</div>`;
};

if (typeof module !== "undefined") module.exports = { logAlertBars, logAlertDefaultName, logAlertStatus, logAlertRow };
