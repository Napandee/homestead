/* Apps that git applies: Flux or Argo CD, or an administrator's mark that
   something else does (#734). A change made here lasts until that tool next
   applies the app, so a review that changes one says so first. managed is
   what the server's reviews answer with: {} or {by, source, detected}. */
function gitopsNote(managed, { rename = false, yaml = "" } = {}) {
  if (!managed?.by) return "";
  const by = esc(managed.by), where = managed.source ? ` (${esc(managed.source)})` : "";
  const body = managed.detected
    ? `${by} applies this app${where}. This change lasts until ${by} next applies it. To keep it, change it in git.`
    : `Marked as updated by ${by}. A change made here can be undone there. To keep it, change it there too.`;
  const tick = rename ? `<label class="switch"><input type="checkbox" id="editManagedConfirm"> ${by} will bring back the old name</label>` : "";
  return UI.callout("warn", rename ? `${managed.detected ? by : "Something else"} applies this app` : "", `<p>${body}</p>${rename
    ? `<p>A rename makes the app under its new name and removes the old one; ${by} then brings the old one back, so there are two copies.</p>${tick}` : ""}${
    gitopsCopyButton(yaml)}`);
}

/* A change made here, as YAML to commit where git keeps the app (#735): the
   server's review sends only the fields that changed, under the app's kind,
   name and namespace. The text is kept here, not in the onclick. */
const GITOPS_COPIES = [];
function gitopsCopyButton(yaml) {
  if (!yaml) return "";
  return UI.button("Copy as YAML for git", `gitopsCopy(${GITOPS_COPIES.push(yaml) - 1})`, { kind: "sm" });
}
window.gitopsCopy = async index => {
  const done = await copyText(GITOPS_COPIES[index] || "");
  toast(done ? "Copied. Paste it where git keeps this app: a kustomize patch, or the same fields in its manifest"
    : "Copying did not work here: select the YAML and copy it", done ? "ok" : "bad");
};
function gitopsYamlBlock(yaml, secret = {}) {
  if (!yaml) return "";
  // The app's own Secret exists only once Homestead has saved the app, never in git.
  const needs = secret?.name ? `<p class="ui-help">It refers to Secret <span class="mono">${esc(secret.name)}</span> (keys ${
    (secret.keys || []).map(key => `<span class="mono">${esc(key)}</span>`).join(", ")}), not to the values. That Secret exists only once
    Homestead has saved the app: in git, create it yourself first (for example with SOPS), or save here first.</p>` : "";
  return UI.more("YAML for git", `<p class="ui-help">Only the fields this change sets. Services and volumes are not in it.
    Values Homestead keeps in the app's Secret, and values with a password or token in them, are left out; still check it
    before committing it to a public repository.</p>${needs}
    <pre class="mono small agent-raw">${esc(yaml)}</pre>${gitopsCopyButton(yaml)}`);
}
window.gitopsCopyButton = gitopsCopyButton;
window.gitopsYamlBlock = gitopsYamlBlock;
window.gitopsNote = gitopsNote;

/* Is Flux applying it? (#742) /api/gitops, read once a minute on the server:
   each Kustomization and HelmRelease, and the apps each applies. */
async function gitopsLoad() {
  STATE.data.gitops = await api("/api/gitops").catch(error => ({ error: error.message }));
  return STATE.data.gitops;
}
const gitopsAgo = iso => { const at = Date.parse(iso || ""); return at ? fmtAgo(Math.max(1, Math.round((Date.now() - at) / 1000))) : ""; };
function gitopsState(row) {
  return row.suspended ? "Suspended" : row.ready === true ? "Ready" : row.ready === false ? "Not ready" : "Applying";
}
/* An app's details: what applies it and how that is going. "" for an app
   Flux does not apply, or before the read has come back. */
function gitopsDetail(w) {
  const report = STATE.data.gitops;
  if (report?.error) {
    // Unread is not fine: an app the update check says Flux applies says so.
    const owner = (STATE.data.imageUpdateMap || {})[`${w.ns}/${w.name}`]?.managed;
    return owner?.by === "Flux" && !window.remoteRow?.(w) ? `<div class="wl-detail-gitops"><span>Flux</span><b>${esc(owner.source || "Flux")}</b>
      <div class="updateerror">Whether Flux is applying it could not be read: ${esc(report.error)}</div></div>` : "";
  }
  const id = report?.apps?.[`${w.ns}/${w.name}`];
  const row = id && (report.objects || []).find(r => r.id === id);
  if (!row || window.remoteRow?.(w)) return "";
  const bits = [gitopsState(row), row.revision, row.tried && `tried ${gitopsAgo(row.tried)}`].filter(Boolean).map(esc).join(" · ");
  return `<div class="wl-detail-gitops"><span>Flux</span><b>${esc(row.id)}</b><div class="dim xs">${bits}</div>${
    row.ready === false && !row.suspended ? `<div class="updateerror">${esc(row.message || "Flux says it is not ready")}</div>` : ""}</div>`;
}
/* The Cluster page: one GitOps line, and what is not Ready with Flux's message. */
async function clusterGitopsPaint() {
  const report = await gitopsLoad(), box = $("#clusterGitops");
  if (!box) return;
  if (report?.error) {
    box.innerHTML = UI.callout("warn", "GitOps", `<p>Flux's Kustomizations and HelmReleases could not be read: ${esc(report.error)}</p>`);
    return;
  }
  if (!(report?.objects || []).length) { box.innerHTML = ""; return; }
  const failing = report.objects.filter(row => row.ready === false && !row.suspended);
  box.innerHTML = UI.callout(failing.length ? "warn" : "info", `GitOps: ${report.summary}`, failing.length
    ? `<ul class="ui-list">${failing.map(row => `<li><b>${esc(row.id)}</b> · not ready${row.since ? ` (${esc(gitopsAgo(row.since))})` : ""}: ${esc(row.message || "no message")}</li>`).join("")}</ul>` : "");
}
window.gitopsLoad = gitopsLoad;
window.gitopsDetail = gitopsDetail;
window.clusterGitopsPaint = clusterGitopsPaint;
