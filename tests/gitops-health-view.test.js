"use strict";
// #742: is Flux applying it? One line on the Cluster page, one on a Flux app's details.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

const REPORT = {
  summary: "2 Kustomizations, 1 HelmRelease: 1 not ready",
  apps: { "lab/web": "Kustomization flux-system/apps", "lab/db": "Kustomization flux-system/infra" },
  objects: [
    { id: "Kustomization flux-system/apps", kind: "Kustomization", namespace: "flux-system", name: "apps", ready: true, suspended: false,
      revision: "main@246b3c6", tried: new Date(Date.now() - 120000).toISOString(), since: "", interval: "10m", message: "Applied revision: main@sha1:246b3c6" },
    { id: "Kustomization flux-system/infra", kind: "Kustomization", namespace: "flux-system", name: "infra", ready: false, suspended: false,
      revision: "main@aaaaaaa", tried: "", since: new Date(Date.now() - 3600000).toISOString(), interval: "10m", message: "kustomize build failed: <missing> secret" },
    { id: "HelmRelease dev/runner", kind: "HelmRelease", namespace: "dev", name: "runner", ready: true, suspended: false,
      revision: "runner-chart 0.15.0", tried: "", since: "", interval: "30m", message: "" }] };

function setup(answer) {
  const elements = { "#clusterGitops": { innerHTML: "" } };
  const c = { window: {}, console, STATE: { data: {} }, $: s => elements[s],
    esc: s => String(s).replaceAll("<", "&lt;").replaceAll(">", "&gt;"), toast() {}, copyText: async () => true,
    fmtAgo: s => `${Math.round(s / 60)} min ago`, remoteRow: () => false,
    api: async path => { assert.equal(path, "/api/gitops"); if (answer instanceof Error) throw answer; return answer; } };
  vm.createContext(c); require("./helpers/load-ui")(c);
  vm.runInContext(fs.readFileSync("web/js/gitops.js", "utf8"), c);
  return { c, elements };
}

test("A Flux app's details say what applies it, that it is Ready and what it applied", async () => {
  const t = setup(REPORT);
  await t.c.window.gitopsLoad();
  const html = t.c.window.gitopsDetail({ ns: "lab", name: "web" });
  assert.match(html, /Kustomization flux-system\/apps/);
  assert.match(html, /Ready/);
  assert.match(html, /main@246b3c6/);
  assert.match(html, /2 min ago/);
});

test("A failing one says so with Flux's message, escaped", async () => {
  const t = setup(REPORT);
  await t.c.window.gitopsLoad();
  const html = t.c.window.gitopsDetail({ ns: "lab", name: "db" });
  assert.match(html, /Not ready/);
  assert.match(html, /&lt;missing&gt; secret/);
});

test("An app Flux does not apply, or before the read, has no line", async () => {
  const t = setup(REPORT);
  assert.equal(t.c.window.gitopsDetail({ ns: "lab", name: "web" }), "");
  await t.c.window.gitopsLoad();
  assert.equal(t.c.window.gitopsDetail({ ns: "lab", name: "plain" }), "");
});

test("The Cluster page has one GitOps line, naming what is not ready", async () => {
  const t = setup(REPORT);
  await t.c.window.clusterGitopsPaint();
  const html = t.elements["#clusterGitops"].innerHTML;
  assert.match(html, /GitOps/);
  assert.match(html, /2 Kustomizations, 1 HelmRelease: 1 not ready/);
  assert.match(html, /Kustomization flux-system\/infra/);
  assert.doesNotMatch(html, /HelmRelease dev\/runner/);
});

test("No Flux, no line; a failed read says what could not be read", async () => {
  const none = setup({ objects: [], apps: {}, summary: "" });
  await none.c.window.clusterGitopsPaint();
  assert.equal(none.elements["#clusterGitops"].innerHTML, "");
  const broken = setup(new Error("Kubernetes said 500"));
  await broken.c.window.clusterGitopsPaint();
  assert.match(broken.elements["#clusterGitops"].innerHTML, /could not be read.*Kubernetes said 500/s);
});
