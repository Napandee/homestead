/* Apps that git applies: Flux or Argo CD, or an administrator's mark that
   something else does (#734). A change made here lasts until that tool next
   applies the app, so a review that changes one says so first. managed is
   what the server's reviews answer with: {} or {by, source, detected}. */
function gitopsNote(managed, { rename = false } = {}) {
  if (!managed?.by) return "";
  const by = esc(managed.by), where = managed.source ? ` (${esc(managed.source)})` : "";
  const body = managed.detected
    ? `${by} applies this app${where}. This change lasts until ${by} next applies it. To keep it, change it in git.`
    : `Marked as updated by ${by}. A change made here can be undone there. To keep it, change it there too.`;
  const tick = rename ? `<label class="switch"><input type="checkbox" id="editManagedConfirm"> ${by} will bring back the old name</label>` : "";
  return UI.callout("warn", rename ? `${managed.detected ? by : "Something else"} applies this app` : "", `<p>${body}</p>${rename
    ? `<p>A rename makes the app under its new name and removes the old one; ${by} then brings the old one back, so there are two copies.</p>${tick}` : ""}`);
}
window.gitopsNote = gitopsNote;
