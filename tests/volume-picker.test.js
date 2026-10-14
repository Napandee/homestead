"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const context = { window: {}, console };
vm.createContext(context);
context.jsArg = s => JSON.stringify(String(s ?? "")); context.jsq = s => (context.esc || String)(context.jsArg(s));
vm.runInContext(fs.readFileSync("web/js/volume-picker.js", "utf8"), context);

const { volumeKind, volumeRowIssue, volumeListIssue } = context.window;

test("stored volumes map onto the picker's storage kinds", () => {
  assert.equal(volumeKind({ kind: "pod" }), "pod");
  assert.equal(volumeKind({ type: "host" }), "host");
  assert.equal(volumeKind({ type: "emptyDir" }), "ephemeral");
  assert.equal(volumeKind({ type: "pvc", create: false }), "existing");
  assert.equal(volumeKind({ type: "pvc", access_mode: "ReadWriteMany" }), "new-rwx");
  assert.equal(volumeKind({}), "new-rwo");
  assert.equal(volumeKind({ kind: "new-rwx", reuse_new: true }), "planned");
});

test("a storage mapping needs an absolute mount path and a source", () => {
  assert.match(volumeRowIssue({ path: "", kind: "existing", source: "media" }), /mount path/);
  assert.match(volumeRowIssue({ path: "config", kind: "existing", source: "media" }), /must start with \//);
  assert.match(volumeRowIssue({ path: "/config", kind: "existing", source: "" }), /needs a storage source/);
  assert.equal(volumeRowIssue({ path: "/config", kind: "existing", source: "media" }), "");
});

test("temporary storage needs no source and new claims need a Kubernetes name", () => {
  assert.equal(volumeRowIssue({ path: "/tmp/cache", kind: "ephemeral", source: "" }), "");
  assert.match(volumeRowIssue({ path: "/config", kind: "new-rwo", source: "Frigate Config" }),
    /lowercase letters, numbers and dashes/);
  assert.equal(volumeRowIssue({ path: "/config", kind: "new-rwx", source: "frigate-config" }), "");
});

test("one container cannot mount two volumes on the same path", () => {
  const rows = [
    { path: "/config", kind: "existing", source: "frigate-config" },
    { path: "/config/", kind: "ephemeral", source: "" },
  ];
  assert.match(volumeListIssue(rows), /mounted twice/);
  assert.equal(volumeListIssue([rows[0], { path: "/media", kind: "pod", source: "config" }]), "");
  assert.equal(volumeListIssue([]), "");
});

test("a folder in a volume stays inside it", () => {
  assert.equal(volumeRowIssue({ path: "/config", kind: "existing", source: "app", sub_path: "config" }), "");
  assert.match(volumeRowIssue({ path: "/config", kind: "existing", source: "app", sub_path: "../x" }), /inside its volume/);
  assert.match(volumeRowIssue({ path: "/config", kind: "existing", source: "app", sub_path: "/etc" }), /inside its volume/);
});

test("a reused new volume must resolve before saving, with the same mount validation", () => {
  assert.match(volumeRowIssue({ path: "/data", kind: "planned", source: "missing", reuse_new: true }), /another mapping/);
  const resolved = { path: "/data", kind: "new-rwo", source: "appdata", reuse_new: true, sub_path: "data" };
  assert.equal(volumeListIssue([{ path: "/config", kind: "new-rwo", source: "appdata" }, resolved]), "");
  assert.match(volumeRowIssue({ ...resolved, sub_path: "../outside" }), /inside its volume/);
  assert.match(volumeRowIssue({ ...resolved, source: "Invalid Name" }), /lowercase/);
});

/* A new pod volume starts on a class several hosts can mount: a migratable
   class mounts on one host at a time, fine for one pod, not for a share. */
test("a new pod volume defaults to the first class that isn't migratable, not to a class picked by name", () => {
  const { defaultStorageClass } = context.window;
  const facts = { "harvester-longhorn": { migratable: true, default: true }, "longhorn-r2": { migratable: true }, longhorn: {} };
  assert.equal(defaultStorageClass(["longhorn", "longhorn-r2"], "", {}), "longhorn");
  assert.equal(defaultStorageClass(["harvester-longhorn", "longhorn"], "", facts), "longhorn");
  assert.equal(defaultStorageClass(["longhorn-r2", "harvester-longhorn"], "", facts), "harvester-longhorn", "all migratable: the cluster's default");
  assert.equal(defaultStorageClass(["longhorn-r2", "longhorn"], "longhorn-r2", facts), "longhorn-r2", "what was asked for is kept");
  assert.equal(defaultStorageClass([], "", {}), "", "no classes: the cluster's own default");
});

test("a migratable class's badge says what it is fit for, not that it is for VM disks only", () => {
  const html = context.window.storageClassBadges({ migratable: true });
  assert.doesNotMatch(html, /VM disks only/);
  assert.match(html, /<span class="tag" data-tip="[^"]*several hosts[^"]*">VM disk class<\/span>/);
});

test("with no classes the picker offers the cluster's default rather than a name it made up", () => {
  const html = context.window.storageClassOptions([], "", {});
  assert.match(html, /<option value="" selected>Cluster default<\/option>/);
  assert.doesNotMatch(html, /longhorn-r2/);
});
