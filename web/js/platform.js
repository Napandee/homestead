/* What this cluster is - Harvester, or plain k3s / RKE2 / Kubernetes - and
   what it has, so pages that need Longhorn or KubeVirt say so instead of
   failing, and the sidebar only offers what can work. */

const PLATFORM_NEEDS = {
  longhorn: { name: "Longhorn", why: "volumes, snapshots and backups are Longhorn's",
    fix: "Homestead can install it here, as it can under Settings → Hardware and storage. Each host needs open-iscsi and an NFS client (nfs-common) first." },
  kubevirt: { name: "KubeVirt", why: "virtual machines run on KubeVirt",
    fix: "Homestead can install it with CDI, which fills VM disks from images - here or under Settings → Hardware and storage. The machines need hardware virtualisation to run VMs at full speed." },
};

async function loadPlatform(force = false) {
  try { STATE.platform = await api(`/api/platform${force ? "?force=1" : ""}`, { keep: true }); } catch (e) { STATE.platform = null; return; }
  const p = STATE.platform;
  // The sidebar offers what this cluster can do.
  const hide = { vms: !p.kubevirt, protect: !p.longhorn };
  Object.entries(hide).forEach(([view, off]) => { const a = $(`#nav a[data-view="${view}"]`); if (a) a.hidden = off; });
}
window.loadPlatform = loadPlatform;

/* k3s's ServiceLB puts every Service on every node's own address: there is
   no VIP to choose, so the forms offer that one choice and say so. */
const nodeAddressesOnly = () => STATE.platform?.load_balancer === "servicelb";
const NODE_ADDRESS_TIP = "k3s's ServiceLB puts every Service on every host's own address, at the LAN port you give it - so each needs a port no other Service uses (Traefik has 80 and 443).";
function nodeAddressOption() {
  return `<option value="shared" selected>Every host's own address · k3s ServiceLB</option>`;
}
/* With kube-vip beside it, ServiceLB still puts a Service on the nodes' own
   addresses: offered as well as a VIP of its own. */
const nodeAddressBeside = () => !!STATE.platform?.servicelb && STATE.platform?.load_balancer === "kube-vip";
function nodeAddressChoice(selected = false) {
  return nodeAddressBeside() ? `<option value="nodes" ${selected ? "selected" : ""}>Every host's own address · ServiceLB</option>` : "";
}
window.nodeAddressBeside = nodeAddressBeside;
window.nodeAddressesOnly = nodeAddressesOnly;

/* true (and the page says why) when this cluster lacks what a page needs. */
function platformLacks(need, title) {
  const p = STATE.platform;
  if (!p || p[need] !== false) return false;
  const info = PLATFORM_NEEDS[need];
  paint(`${UI.pageHeader(`${esc(title)}`, `${esc(platformName(p))} · ${esc(info.name)} is not installed`, ``)}
    <div class="empty platform-missing"><b>This page needs ${esc(info.name)}</b>, because ${esc(info.why)}.
      <p class="dim small" style="max-width:560px;margin:10px auto 0">${esc(info.fix)}</p>
      ${can("admin") && p.helm_controller ? `<div class="row" style="justify-content:center;margin-top:12px">
        <button class="btn pri" onclick="addonInstall(${jsq(need)})">Install ${esc(info.name)}</button></div>` : ""}</div>`);
  return true;
}
window.platformLacks = platformLacks;

function platformName(p) {
  return { harvester: "Harvester", k3s: "k3s", rke2: "RKE2", kubernetes: "Kubernetes" }[p?.distribution] || "Kubernetes";
}
window.platformName = platformName;

/* ---------- add-ons: Longhorn and KubeVirt where the cluster lacks them ----------
   Installed through the Helm controller k3s and RKE2 run, so they are
   ordinary HelmCharts afterwards. Harvester brings both. */
const ADDONS = {
  longhorn: { name: "Longhorn", what: "Replicated volumes, snapshots and backups - the Volumes and Data protection pages",
    needs: "Each host needs open-iscsi and an NFS client (nfs-common) installed and iscsid running; the k3s script does that. Volumes keep one copy per host, up to three." },
  "kube-vip": { name: "kube-vip", what: "VIPs for apps: a container can have a LAN address of its own, handed out from Networking > Your VIPs, as on Harvester",
    needs: "Announces each VIP from one host with ARP. Beside k3s's ServiceLB it takes only the Services given a VIP; the rest stay on the hosts' own addresses." },
  multus: { name: "Multus", what: "Second networks for pods - what LAN networks need, so a container or VM can have an address of its own on your LAN",
    needs: "Installed on every host from the chart RKE2 uses for it; apps already running are left as they are." },
  kubevirt: { name: "KubeVirt", what: "Virtual machines, with CDI to fill their disks from images",
    needs: "The newest KubeVirt and CDI releases are installed. VMs run at full speed where a host has hardware virtualisation (/dev/kvm); without it KubeVirt emulates, many times slower." },
};

/* What Homestead needs under it on k3s and RKE2 - kube-vip for VIPs, Multus
   for a VM's or container's own LAN address - and one button for whatever is
   missing. A new installation gets them by itself; an older one is asked
   here, on Add-ons and on Networking. Empty when nothing is missing. */
function baselineHtml(r, where = "addons") {
  if (!r?.applies || !(r.missing || []).length) return "";
  const missing = r.parts.filter(p => p.missing);
  return `<div class="note warn baseline-note" style="margin-bottom:12px"><b>Required components not installed</b>
    <ul class="baseline-list">${missing.map(p => `<li><b>${esc(p.name)}</b> · ${esc(p.why)}</li>`).join("")}</ul>
    <div class="dim xs">Installed with the ${esc(platformName({ distribution: r.distribution }))} Helm controller at tested versions. Upgrades are available under System → Cluster → Platform versions.
      ${where === "network" ? "Until installed, applications are published on host addresses only." : ""}</div>
    ${can("admin") ? `<div class="row" style="margin-top:8px"><button class="btn sm pri" onclick="baselineInstall()">Install components</button></div>` : '<div class="dim xs">Installation requires the admin role.</div>'}</div>`;
}
window.baselineHtml = baselineHtml;

const BASELINE_WORDS = {
  multus: "Added to the CNI configuration on every host. Running pods are not affected.",
  macvtap: "Runs on every host and lets VMs join the LAN through the host's network interface. The hosts' own network is not changed.",
  "kube-vip": "Runs on every host and announces each virtual IP from one host. Existing services are not changed.",
};

/* In Homestead's own dialog, not the browser's: a browser can silence its
   confirm boxes, and then the button did nothing at all. */
window.baselineInstall = async () => {
  const open = window.childModal && !$("#modal").classList.contains("hidden") ? childModal : modal;
  open("Install required components", '<div class="empty"><span class="spin2"></span> Checking what is missing…</div>');
  const r = await api("/api/platform/baseline").catch(e => ({ error: e.message }));
  const missing = (r?.parts || []).filter(p => p.missing);
  if (r?.error || !missing.length) {
    $("#mbody").innerHTML = (r?.error ? UI.callout("bad", "Could not check the components.", esc(r.error)) : UI.callout("ok", "All required components are installed.", ""))
      + UI.actions(UI.cancel("Close"));
    return;
  }
  $("#mbody").innerHTML = `<div class="ui-stack">
    <ul class="ui-list">${missing.map(p => `<li><b>${esc(p.name)}</b> - ${esc(BASELINE_WORDS[p.id] || p.why)}</li>`).join("")}</ul>
    <p class="dim xs">Installed through the Helm controller at the versions Homestead has tested; follow each under Jobs.</p>
    <div id="bl_result"></div>
    ${UI.actions(UI.cancel() + UI.button("Install", "baselineInstallGo()", { kind: "pri", id: "bl_go", attrs: 'data-need="admin"' }))}</div>`;
  window.__baselineMissing = missing.map(p => p.id);
};
window.baselineInstallGo = async () => {
  const go = $("#bl_go");
  if (go) { go.disabled = true; go.textContent = "Installing…"; }
  try {
    const out = await api("/api/platform/baseline/install", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ parts: window.__baselineMissing || [] }) });
    $("#bl_result").innerHTML = (out.results || []).map(row => UI.callout(row.ok ? "ok" : "bad",
      `${esc(row.id)}: ${row.ok ? "installing" : "not installed"}`, esc(row.detail || ""))).join("") || UI.callout("ok", esc(out.detail || "Done"), "");
    if (go) { go.textContent = out.ok ? "Started" : "Try again"; go.disabled = out.ok; }
    toast(out.detail, out.ok ? "ok" : "bad");
    if (window.refreshOperations) refreshOperations(true);
    if ($("#addonsCard")) addonsPaint();
  } catch (e) {
    $("#bl_result").innerHTML = UI.callout("bad", "The install request failed.", esc(e.message));
    if (go) { go.disabled = false; go.textContent = "Try again"; }
  }
};

// The recovery state in one line, and how the server works in UI.more, as the
// SMB card does. `lead` opens the explanation, where a page has more to say.
// The chip says lock recovery is off when the state line doesn't already.
window.nfsRecovery = (state, expanded = false, lead = "") => {
  const recovery = state?.recovery;
  if (!recovery) return `<div class="dim xs" style="margin-top:8px">Node-failure recovery has not been checked.</div>${lead ? UI.more("How the NFS server works", lead) : ""}`;
  const issues = [...(recovery.blockers || []), ...(recovery.warnings || [])];
  return `<div class="row small" style="margin-top:8px;gap:6px;flex-wrap:wrap"><span>Recovery: ${esc(recovery.detail || "not checked")}</span>${recovery.lock_recovery === false && !/lock recovery/i.test(recovery.detail || "") ? UI.chip("No lock recovery", "warn") : ""}</div>
    ${UI.more("How the NFS server works", `${lead}<p>${(recovery.eligible_hosts || []).length} eligible NFS hosts${recovery.eligible_hosts?.length ? ` · ${esc(recovery.eligible_hosts.join(", "))}` : " · enable NFS server support on the hosts and update the node probe"}. Access pauses while the server, VIP and storage recover.</p>
    ${issues.length ? `<ul>${issues.map(issue => `<li>${esc(issue)}</li>`).join("")}</ul>` : ""}`, expanded)}`;
};

window.addonsPaint = async () => {
  const card = $("#addonsCard");
  if (!card) return;
  let s, health, nfs, baseline, consoles;
  try { [s, health, nfs, baseline, consoles] = await Promise.all([api("/api/addons"), api("/api/self/health").catch(() => ({})),
    api("/api/shares/nfs/server").catch(error => ({ error: error.message })), api("/api/platform/baseline").catch(() => null),
    can("admin") ? api("/api/host-console").catch(() => null) : null]); }
  catch (e) { card.hidden = true; return; }
  card.hidden = false;
  const probe = health.probe || {}, smb = health.samba || {};
  STATE.data.sambaInstalled = !!smb.installed;
  const kvmLine = !s.kvm_known ? '<span class="dim">The node probe has not said whether the hosts have hardware virtualisation.</span>'
    : s.kvm_everywhere ? "Every host has hardware virtualisation."
    : s.kvm_nowhere ? '<b>No host has hardware virtualisation</b>: KubeVirt will emulate, and VMs run slowly.'
    : `Only ${Object.entries(s.kvm).filter(([, on]) => on).map(([n]) => esc(n)).join(", ")} ha${Object.values(s.kvm).filter(Boolean).length === 1 ? "s" : "ve"} hardware virtualisation; VMs run there.`;
  const row = (key, state) => {
    const a = ADDONS[key];
    const runtime = key === "multus" && state.state;
    const pill = runtime ? `<span class="pill ${state.ready ? "ok" : "med"}">${esc(state.state)}</span>` : state.installed ? '<span class="pill ok">installed</span>'
      : state.installing ? '<span class="pill low">installing</span>' : '<span class="pill">not installed</span>';
    const button = state.repairable ? '<button class="btn sm pri" data-need="admin" onclick="multusRepair()">Repair configuration</button>' : state.installed || state.installing ? ""
      : s.helm_controller ? `<button class="btn sm pri" data-need="admin" onclick="addonInstall(${jsq(key)})">Install ${esc(a.name)}</button>`
      : '<span class="dim xs">needs the Helm controller k3s and RKE2 run</span>';
    const diagnostic = key === "multus" && !state.ready && state.diagnostic_command
      ? `<div class="note warn addon-diagnostic"><b>${esc(state.detail || "Multus is not ready")}</b> ${esc((state.issues || []).join("; "))} SSH to a server host to gather Helm, node-agent status and logs:</div>
         ${guideCopy(state.diagnostic_command)}` : "";
    const emulation = key === "kubevirt" && state.installed && state.emulation != null
      ? `<div class="dim xs" style="margin-top:4px">${state.emulation
          ? "Software fallback is allowed when /dev/kvm is missing."
          : "Hardware virtualisation (/dev/kvm) is required."}</div>` : "";
    const emulationButton = key === "kubevirt" && state.installed && !state.emulation && !s.kvm_everywhere && can("admin")
      ? '<button class="btn sm" onclick="kubevirtEmulation(true)">Allow software fallback</button>' : "";
    return `<div class="addon-row"><div><b>${esc(a.name)}</b> ${pill}<div class="dim small">${esc(a.what)}</div>
        ${state.installed ? "" : `<div class="dim xs" style="margin-top:4px">${esc(a.needs)}</div>`}
        ${key === "kubevirt" ? `<div class="xs" style="margin-top:4px">${kvmLine}</div>${emulation}` : ""}
        ${key === "multus" && state.ready ? `<div class="dim xs">${esc(state.detail)}</div>` : ""}
        ${diagnostic}</div>
      <div class="row">${button}${emulationButton}</div></div>`;
  };
  const probePill = !probe.installed ? '<span class="pill">not installed</span>'
    : probe.ready < probe.desired || probe.reporting < probe.desired ? `<span class="pill low">${probe.ready || 0}/${probe.desired || 0} ready</span>`
    : '<span class="pill ok">installed</span>';
  const probeButton = !can("admin") ? "" : !probe.installed
    ? '<button class="btn sm pri" onclick="probeInstallConfirm()">Install node probe</button>'
    : '<button class="btn sm" onclick="allocationProbeSettings()">VM placement checks</button><button class="btn sm" onclick="probeRemove()">Remove</button>';
  const probeRow = `<div class="addon-row"><div><b>Node probe</b> ${probePill}
      <div class="dim small">Host hardware, /dev/kvm, temperatures, physical disks, SMART health and per-disk throughput</div>
      <div class="dim xs" style="margin-top:4px">A lightweight read-only probe runs on every host; SMART tests use its separate privileged sidecar.</div></div>
    <div class="row">${probeButton}</div></div>`;
  const smbPill = !smb.installed ? '<span class="pill">not installed</span>'
    : !smb.enabled ? '<span class="pill med">off</span>'
    : smb.ready < smb.desired ? '<span class="pill low">starting</span>'
    : smb.partial ? '<span class="pill med">partial service</span>' : '<span class="pill ok">serving</span>';
  const smbRow = `<div class="addon-row"><div><b>SMB network shares</b> ${smbPill}
      <div class="dim small">Windows, macOS and Linux file shares from the saved Network Shares inventory</div>
      <div class="dim xs" style="margin-top:4px">${smb.shares || 0} configured · ${smb.address ? esc(smb.address) : "address not assigned"} · stopping or removing SMB keeps all PVCs and share settings</div></div>
    <div class="row"><button class="btn sm" onclick="go('shares')">Shares</button>
      ${can("admin") ? actionBar([{ label: smb.enabled ? "Disable" : "Enable", run: `sambaSet(${!smb.enabled})` },
        smb.installed && { label: "Remove server", run: "sambaRemove()", danger: true }].filter(Boolean), { shown: 1, label: "SMB server" }) : ""}</div></div>`;
  STATE.data.nfs = nfs;
  const nfsPill = nfs.error ? '<span class="pill crit">status unavailable</span>' : !nfs.installed ? '<span class="pill">not installed</span>'
    : !nfs.enabled ? '<span class="pill med">off</span>'
    : nfs.ready < nfs.desired ? '<span class="pill low">starting</span>' : '<span class="pill ok">serving</span>';
  const nfsRow = `<div class="addon-row"><div><b>NFSv4 network shares</b> ${nfsPill}
      <div class="dim small">A separate, opt-in NFS container serving selected RWX shares to allowed client networks</div>
      <div class="dim xs" style="margin-top:4px">${(nfs.exports || []).length} exports · ${nfs.address ? esc(nfs.address) : "address not assigned"} · removing the server keeps the shares and volumes</div>${nfsRecovery(nfs)}</div>
    <div class="row"><button class="btn sm" onclick="go('shares')">Exports</button>
      ${can("admin") && !nfs.error ? actionBar([{ label: nfs.enabled ? "Disable" : "Enable", run: `nfsSet(${!nfs.enabled})` },
        nfs.installed && { label: "Remove server", run: "nfsRemove()", danger: true }].filter(Boolean), { shown: 1, label: "NFS server" }) : ""}</div></div>`;
  const clusterRows = s.harvester ? "" : `${hostConsoleRow(consoles)}${row("longhorn", s.longhorn)}${row("kubevirt", s.kubevirt)}${s.multus ? row("multus", s.multus) : ""}${s.kube_vip ? row("kube-vip", s.kube_vip) : ""}`;
  card.innerHTML = `${UI.moduleHeader(`Add-ons`, `${s.harvester ? "Optional Homestead services; Harvester already provides storage, VM and network add-ons"
        : `What this ${esc(platformName(STATE.platform || { distribution: s.distribution }))} cluster can add`}`, ``)}
    ${baselineHtml(baseline)}${probeRow}${smbRow}${nfsRow}${clusterRows}`;
  if (window.applyRole) applyRole();
};

/* The host console: each host's local status screen, on or off for the
   whole cluster. Homestead installs it where it is missing - hosts that join
   later too - updates it when Homestead carries a newer one, and removes it
   when turned off, a couple of hosts every ten minutes. */
function hostConsoleRow(c) {
  if (!c || c.harvester) return "";
  const hosts = c.nodes.filter(n => !n.native);
  const pill = c.enabled === null ? '<span class="pill">not checked yet</span>'
    : !c.enabled ? (c.installed ? `<span class="pill low">removing · ${c.installed} left</span>` : '<span class="pill">off</span>')
    : c.settled ? '<span class="pill ok">installed</span>'
    : `<span class="pill low">${c.current} of ${c.hosts} up to date</span>`;
  // A host behind this release, or one that failed, can be done now rather
  // than on the next pass (failed hosts wait an hour between tries).
  const behind = n => c.enabled && n.ready && (!n.enabled || n.current === false || /failed/.test(n.detail || ""));
  const lines = hosts.map(n => `<div class="xs between" style="gap:8px"><span><b>${esc(n.name)}</b> <span class="dim">${esc(n.detail || (n.ready ? "not checked yet" : "not Ready"))}</span></span>
    ${behind(n) && can("admin") ? `<button class="btn sm" onclick="hostConsoleNow(${jsq(n.name)},this)">Update now</button>` : ""}</div>`).join("");
  return `<div class="addon-row"><div><b>Host console</b> ${pill}
      <div class="dim small">Each host's local screen: CPU, memory, disks, addresses and the cluster's health, before the login prompt</div>
      <div class="dim xs" style="margin-top:4px">Bundled with Homestead v${esc(c.version)}; installed on new hosts and updated with Homestead, a couple of hosts every ten minutes.
        A host shows the new one at once, or after a logout if someone is signed in on its screen. Harvester keeps its own console.</div>
      <details class="small" style="margin-top:6px"><summary>${hosts.length} host${hosts.length === 1 ? "" : "s"}</summary>${lines}</details></div>
    <div class="row">${can("admin") ? actionBar([{ label: c.enabled ? "Disable" : "Enable", run: `hostConsoleSet(${!c.enabled})` }], { label: "Host console" }) : ""}</div></div>`;
}

window.hostConsoleNow = async (node, button) => {
  button.disabled = true; button.textContent = "Updating…";
  try {
    await api("/api/host-console", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ node, action: "enable" }) });
    toast(`Updating the console on ${node}: follow it in the bell`, "ok");
  } catch (e) { toast(e.message, "bad"); button.disabled = false; button.textContent = "Update now"; }
};

window.hostConsoleSet = async on => {
  if (!on && !(await ask("Remove the host console from every host? Each goes back to the plain login prompt after its console logs out or it restarts.", { title: "Remove the host console", ok: "Remove", danger: true }))) return;
  try {
    const r = await api("/api/host-console", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: on }) });
    toast(r.detail, "ok");
    addonsPaint();
  } catch (e) { toast(e.message, "bad"); }
};

window.kubevirtEmulation = async enabled => {
  if (enabled && !(await ask("Allow KubeVirt's software fallback? VMs can then start on hosts without /dev/kvm, but run much more slowly there. Running VMs are not restarted.", { title: "Allow software emulation", ok: "Allow" }))) return;
  try {
    const result = await api("/api/addons/kubevirt/emulation", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }) });
    toast(result.detail, "ok");
    addonsPaint();
  } catch (e) { toast(e.message, "bad"); }
};

window.multusRepair = async () => {
  if (!(await ask("Correct Multus's k3s CNI directory? This rolls its node agents. Existing apps and VMs are not restarted; new pod networking may briefly pause during the rollout.", { title: "Correct Multus's CNI directory", ok: "Correct it" }))) return;
  try {
    const r = await api("/api/addons/multus/repair", { method: "POST", body: "{}", headers: { "Content-Type": "application/json" } });
    toast(r.detail, "ok");
    refreshOperations(true); addonsPaint();
  } catch (e) { toast(e.message, "bad"); }
};

window.addonInstall = async key => {
  const a = ADDONS[key];
  let body = {};
  if (key === "kubevirt") {
    const s = await api("/api/addons").catch(() => ({}));
    if (s.kvm_nowhere && !(await ask("No host has hardware virtualisation (/dev/kvm), so KubeVirt will emulate: VMs work, but many times slower. Install anyway?", { title: "Install KubeVirt without KVM", ok: "Install anyway" }))) return;
    body = {};
  } else if (key === "kube-vip") {
    const s = await api("/api/addons").catch(() => ({}));
    const where = s.kube_vip?.interface ? `It announces VIPs on ${s.kube_vip.interface}, the interface each host's default route uses.`
      : "The hosts did not agree on one network interface (or the node probe has not said), so kube-vip finds it itself.";
    if (!(await ask(`Install kube-vip? ${a.needs} ${where}`, { title: "Install kube-vip", ok: "Install" }))) return;
  } else if (!(await ask(`Install ${a.name}? ${a.needs}`, { title: `Install ${a.name}`, ok: "Install" }))) return;
  try {
    const r = await api(`/api/addons/${key}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(r.detail || `${a.name} is being installed`, "ok");
    if (window.refreshOperations) refreshOperations(true);
    addonsPaint();
    // The pages that need it appear once it is there.
    const watch = setInterval(async () => {
      await loadPlatform(true);
      if (key === "kube-vip" ? STATE.platform?.load_balancer === "kube-vip" : STATE.platform?.[key]) {
        clearInterval(watch);
        toast(key === "multus" ? "Multus is ready - LAN networks can be made now"
          : key === "kube-vip" ? "kube-vip is ready - apps can be given VIPs now" : `${a.name} is ready - its pages are in the sidebar`, "ok");
        addonsPaint();
      }
    }, 20000);
    setTimeout(() => clearInterval(watch), 20 * 60000);
  } catch (e) { toast(e.message, "bad"); }
};

/* Adding a host to a k3s or RKE2 cluster: the distribution's own installer,
   this cluster's address, and where the token lives - which Homestead
   cannot read, since it is a file on the server, not in the API. */
/* What each role does, and its command (homestead_platform.join_guide). */
const JOIN_ROLES = [
  ["app", "App host", "Runs apps and can hold storage."],
  ["control-plane", "App host + control plane", "Also holds an etcd vote: three servers keep the cluster up through the loss of one."],
  ["tiebreaker", "Tiebreaker (votes only)", "Holds an etcd vote and nothing else: no apps, no storage. A small, reliable machine on its own power - a Raspberry Pi beside two main hosts."],
];
const INSTALLER = "https://raw.githubusercontent.com/homestead-lab/homestead/main/scripts/install.sh";
function joinCommandHtml(g, role) {
  const k3s = g.distribution === "k3s";
  // RKE2's guide installs an agent; a server holding a vote joins through the
  // installer, which writes the role's config and checks the machine first.
  if (!k3s && role !== "app") {
    return `<p class="dim xs">On RKE2 the installer joins a ${role === "tiebreaker" ? "tiebreaker" : "server"}: it asks for this cluster's address and token, checks the machine, and writes the role's settings.</p>`
      + guideCopy(`curl -sfL ${INSTALLER} | sudo HS_ROLE=${role === "tiebreaker" ? "tiebreaker" : "server"} sh`);
  }
  const cmd = k3s && role === "tiebreaker" ? g.tiebreaker : k3s && role === "control-plane" ? g.server_join : g.agent;
  const shared = role !== "app" && k3s && !g.settings_known
    ? `<p class="dim xs">Homestead could not read the other servers' settings. Copy these from the first server's <span class="mono">/etc/rancher/k3s/config.yaml</span> or its service file, if it sets them: <span class="mono">${esc((g.shared_flags || []).join(" "))}</span>.</p>` : "";
  const check = role !== "app" && g.check
    ? `<p class="dim xs">First check the machine can hold a vote - its disk, cgroups, network module, swap and power:</p>${guideCopy(role === "tiebreaker" ? g.check : g.check.replace("HS_ROLE=tiebreaker", "HS_ROLE=server"))}` : "";
  return check + guideCopy(cmd) + (k3s ? "" : `<p class="dim xs">with <span class="mono">/etc/rancher/rke2/config.yaml</span> saying:</p>${guideCopy(g.config || "")}`) + shared;
}
window.joinCommandHtml = joinCommandHtml;
window.platformJoinRole = role => {
  const g = STATE.data.joinGuide;
  if (g && $("#joinCmd")) $("#joinCmd").innerHTML = joinCommandHtml(g, role);
};
window.platformJoinGuide = async (role = "app") => {
  modal("Add a host", '<div class="empty"><span class="spin2"></span>reading the cluster</div>', true);
  let g;
  try { g = await api("/api/platform/join"); } catch (e) { $("#mbody").innerHTML = `<div class="note bad">${esc(e.message)}</div>`; return; }
  STATE.data.joinGuide = g;
  const k3s = g.distribution === "k3s";
  if (!["k3s", "rke2"].includes(g.distribution)) {
    $("#mbody").innerHTML = `<p class="muted small">This cluster's distribution could not be told from its hosts, so Homestead cannot give its join steps. Use the installer that set the cluster up.</p>`;
    return;
  }
  $("#mbody").innerHTML = `<p class="muted small">This cluster runs <b>${k3s ? "k3s" : "RKE2"} v${esc(g.version)}</b>; the new host joins with the same release. Run these on the new machine, as root or with sudo.</p>
    <section class="guide-step"><h4><span>1</span>Read the join token</h4>
      <p>It is on the server${g.server ? ` at <span class="mono">${esc(g.server)}</span>` : ""}; Homestead cannot read it for you.</p>
      ${guideCopy(`sudo cat ${g.token_file}`)}
      <p class="dim xs">Anyone holding it can join a machine to this cluster, so keep it out of chats and notes.</p></section>
    <section class="guide-step"><h4><span>2</span>If it will hold Longhorn volumes</h4>${guideCopy(g.longhorn)}</section>
    <section class="guide-step"><h4><span>3</span>Choose what it does, and join it</h4>
      <div class="answer-modes">${JOIN_ROLES.map(([value, label, help]) => `<label class="answer-mode"><input type="radio" name="joinRole" value="${value}"${value === role ? " checked" : ""}
        onchange="platformJoinRole(this.value)"><span><b>${esc(label)}</b><span class="dim small">${esc(help)}</span></span></label>`).join("")}</div>
      <div id="joinCmd">${joinCommandHtml(g, role)}</div></section>
    <section class="guide-step"><h4><span>4</span>Watch it join</h4><p class="dim small">It appears on the Hosts page within a minute or two, and turns Ready once its networking is up.</p></section>`;
};
