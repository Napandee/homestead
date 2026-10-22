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
function gitopsYamlBlock(yaml) {
  if (!yaml) return "";
  return UI.more("YAML for git", `<p class="ui-help">Only the fields this change sets. Services and volumes are not in it.</p>
    <pre class="mono small agent-raw">${esc(yaml)}</pre>${gitopsCopyButton(yaml)}`);
}
window.gitopsCopyButton = gitopsCopyButton;
window.gitopsYamlBlock = gitopsYamlBlock;
window.gitopsNote = gitopsNote;
