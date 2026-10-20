// A read that failed is not an empty answer (#620): a page says it could not
// read, and offers nothing that acts on a guess.
import assert from "node:assert/strict";
import {chromium} from "playwright";
const base = process.env.HOMESTEAD_URL || "http://127.0.0.1:4176";
const browser = await chromium.launch({headless: true});

// Fail one API path the way a timeout does; every other read goes through.
// Each call wraps the page's own api(), so only the latest path fails.
const failing = path => {
  window.__realApi = window.__realApi || window.api;
  const read = window.__realApi;
  window.api = (asked, ...rest) => String(asked).split("?")[0] === path
    ? Promise.reject(new Error("timed out")) : read(asked, ...rest);
};
const settled = page => page.waitForFunction(() => !document.querySelector("#mbody .spin2, #mbody .loading"));

try {
  for (const width of [1440, 390]) {
    const context = await browser.newContext({viewport: {width, height: 1000}});
    const page = await context.newPage(), errors = [];
    page.on("pageerror", e => errors.push(e.message));
    await page.goto(`${base}/?demo=1`);
    await page.waitForSelector(".dashboard-widget");

    // Remove a host: unread hosts are not "every host is Ready".
    await page.evaluate(failing, "/api/nodes");
    await page.evaluate(() => clusterRemovePick());
    await page.waitForFunction(() => !document.querySelector("#mbody .spin2"));
    const hosts = await page.locator("#mbody").innerText();
    assert.match(hosts, /Could not read the hosts/);
    assert.match(hosts, /timed out/);
    assert.doesNotMatch(hosts, /Every host is Ready/);
    await page.evaluate(() => closeModal());

    // Data protection: unread backup storage is unknown, and "Set up" is not offered.
    await page.evaluate(failing, "/api/objectstore");
    await page.evaluate(() => viewProtect());
    await page.waitForFunction(() => document.querySelector(".phead")?.innerText.includes("Data protection"));
    const protect = await page.locator("body").innerText();
    assert.match(protect, /Backup storage\s*unknown/i);
    assert.equal(await page.getByRole("button", {name: "Set up storage"}).count(), 0, "Set up is not offered on a guess");
    assert.doesNotMatch(protect, /No backup storage/);

    // Volumes: a part that could not be read is named, not silently left out.
    await page.evaluate(failing, "/api/volumes/old-copies");
    await page.evaluate(() => viewStorage());
    await page.waitForFunction(() => document.querySelector(".phead")?.innerText.includes("Volumes"));
    assert.match(await page.locator("body").innerText(), /could not be read[\s\S]*old copies[\s\S]*timed out/i);

    // Create volume: no form that would make it on whatever class the server picks.
    await page.evaluate(failing, "/api/storageclasses");
    await page.evaluate(() => volumeCreate());
    await settled(page);
    const create = await page.locator("#mbody").innerText();
    assert.match(create, /Could not read the storage classes[\s\S]*timed out/);
    assert.equal(await page.locator("#mbody").getByRole("button", {name: "Create volume"}).count(), 0);
    await page.evaluate(() => closeModal());

    // Ownership: unread is not "nothing here declares a user".
    await page.evaluate(failing, "/api/volumes/ownership");
    await page.evaluate(() => volumeChown("lab", "frigate-config"));
    await settled(page);
    const owner = await page.locator("#mbody").innerText();
    assert.match(owner, /Could not read what uses this volume[\s\S]*timed out/);
    assert.doesNotMatch(owner, /Nothing here declares a user/);
    await page.evaluate(() => closeModal());

    // New storage class: unread disks are not "no disk has a tag yet".
    await page.evaluate(failing, "/api/disks");
    await page.evaluate(() => { STATE.data.disks = null; storageClassCreate(); });
    await page.waitForFunction(() => !document.querySelector("#sc_disktags")?.innerText.includes("loading"));
    const tags = await page.locator("#sc_disktags").innerText();
    assert.match(tags, /Could not read the disks[\s\S]*timed out/);
    assert.doesNotMatch(tags, /No disk has a tag yet/);
    await page.evaluate(() => closeModal());

    // Settings: the storage classes card says it could not read them, rather than vanish.
    await page.evaluate(() => { window.api = window.__realApi; settingsTab("cluster"); go("settings"); });
    await page.waitForSelector("#storageClassesCard", {state: "attached"});
    await page.evaluate(failing, "/api/storage/classes");
    await page.evaluate(() => storageClassesPaint());
    const card = page.locator("#storageClassesCard");
    assert.equal(await card.isVisible(), true, "the card stays");
    assert.match(await card.innerText(), /Could not read the storage classes[\s\S]*timed out/);

    // VMs and imports (views-lifecycle.js).
    const body = () => page.locator("#mbody").innerText();
    const close = () => page.evaluate(() => closeModal());

    // New LAN network: unread options are not "Multus is needed first".
    await page.evaluate(() => { window.api = window.__realApi; window.__vmCreateOptions = undefined; });
    await page.evaluate(failing, "/api/vm/create-options");
    await page.evaluate(() => vmNetworkAdd());
    await settled(page);
    assert.match(await body(), /Could not read the network options[\s\S]*timed out/);
    assert.doesNotMatch(await body(), /Multus is needed first/);
    await close();

    // New VM: no form built on invented options.
    await page.evaluate(() => vmNew());
    await settled(page);
    assert.match(await body(), /Could not read what a new VM can use[\s\S]*timed out/);
    assert.equal(await page.locator("#v_steps").count(), 0);
    await close();

    // Remove an import: no removal offered without knowing what it created.
    await page.evaluate(failing, "/api/imports/cleanup-plan");
    await page.evaluate(() => importRemove("media-server", "succeeded"));
    await settled(page);
    assert.match(await body(), /Could not check what this import created[\s\S]*timed out/);
    assert.equal(await page.locator("#imr_workload, .imr-volume").count(), 0);
    await close();

    // A source's Docker containers that could not be listed say so.
    await page.evaluate(failing, "/api/sources/containers");
    await page.evaluate(async () => { STATE.data.srcs = await window.__realApi("/api/sources"); await srcBrowse("unraid"); });
    await page.waitForFunction(() => !document.querySelector("#mbody .spin2"));
    assert.match(await body(), /Could not list the Docker containers on unraid[\s\S]*timed out/);
    await close();

    // Import: no invented storage class or namespace list.
    await page.evaluate(failing, "/api/storageclasses");
    await page.evaluate(() => viewImport());
    await page.waitForFunction(() => document.querySelector(".phead")?.innerText.includes("Import"));
    assert.match(await page.locator("body").innerText(), /could not be read[\s\S]*storage classes[\s\S]*timed out/i);
    // Virtual machines › Import, where Import VM disk is offered: the same.
    await page.evaluate(() => viewVmImport());
    await page.waitForFunction(() => document.querySelector(".phead")?.innerText.includes("Virtual machines"));
    assert.match(await page.locator("body").innerText(), /could not be read[\s\S]*storage classes[\s\S]*timed out/i);
    await page.evaluate(() => vmDiskImport());
    assert.match(await body(), /Could not read the storage classes/);
    assert.equal(await page.locator("#vd_name").count(), 0);
    await close();

    // A VM from an Unraid server: no invented options or class.
    await page.evaluate(failing, "/api/vm/create-options");
    await page.evaluate(() => {
      STATE.data.importStorageClasses = null;
      STATE.data.uvms = { unraid: { vms: [{ id: "v1", name: "vm1", slug: "vm1" }] } };
      return uvmImport("unraid", "v1");
    });
    await settled(page);
    assert.match(await body(), /Could not read what this import needs[\s\S]*timed out/);
    await close();

    // The import wizard's storage step: no form when the storage options are unread.
    await page.evaluate(failing, "/api/deploy/options");
    await page.evaluate(() => importSetup("unraid", "media-server"));
    await settled(page);
    assert.match(await body(), /Could not read the storage options[\s\S]*timed out/);
    assert.equal(await page.locator("#im_steps").count(), 0);
    await close();

    // Edit an app: its placement, storage, neighbours and address come from
    // these reads; saving a form built without one would change what it was.
    for (const [path, what] of [["/api/nodes", "the hosts"], ["/api/deploy/options", "the storage options"],
      ["/api/workloads", "the other apps"], ["/api/network", "the network"]]) {
      await page.evaluate(() => { window.api = window.__realApi; });
      await page.evaluate(failing, path);
      // In one step, so a refresh cannot refill the list of apps in between.
      await page.evaluate(() => { STATE.data.wl = null; wlEdit("lab", "frigate", true); });
      await settled(page);
      assert.match(await body(), new RegExp(`Could not read ${what}[\\s\\S]*timed out`));
      assert.equal(await page.locator("#e_steps").count(), 0, `no edit form without ${what}`);
      await close();
    }

    // Deploy: no namespace, host list, storage or addresses made up for the form.
    for (const [path, what] of [["/api/namespaces", "the namespaces"], ["/api/nodes", "the hosts"],
      ["/api/deploy/options", "the storage options"], ["/api/network", "the network"]]) {
      await page.evaluate(() => { window.api = window.__realApi; });
      await page.evaluate(failing, path);
      await page.evaluate(() => go("deploy"));
      await page.waitForFunction(() => /Could not read|Basics/.test(document.querySelector("#views")?.innerText || ""));
      const deploy = await page.locator("#views").innerText();
      assert.match(deploy, new RegExp(`Could not read ${what}[\\s\\S]*timed out`));
      assert.equal(await page.locator("#d_target_mode").count(), 0, `no deploy form without ${what}`);
    }
    // Retry after a failed read opens the template that was asked for, not an empty form.
    await page.evaluate(() => { window.api = window.__realApi; });
    await page.evaluate(failing, "/api/namespaces");
    await page.evaluate(() => { window.__deployPrefill = { name: "from-template" }; go("deploy"); });
    await page.waitForFunction(() => /Could not read the namespaces/.test(document.querySelector("#views")?.innerText || ""));
    await page.evaluate(() => { window.api = window.__realApi; go("deploy"); });
    await page.waitForSelector("#d_target_mode");
    assert.equal(await page.evaluate(() => DCFG.workload_name), "from-template");

    // Import Docker Compose and New VM: no made-up namespace list.
    for (const [path, what] of [["/api/namespaces", "the namespaces"], ["/api/network", "the network"]]) {
      await page.evaluate(() => { window.api = window.__realApi; });
      await page.evaluate(failing, path);
      await page.evaluate(() => composeImport());
      await settled(page);
      assert.match(await body(), new RegExp(`Could not read ${what}[\\s\\S]*timed out`));
      assert.equal(await page.locator("#composeNs").count(), 0, `no Compose form without ${what}`);
      await close();
    }
    // New VM refuses where a guess would build the wrong VM; where a failed read
    // only hides some choices, the form opens and says what it could not read.
    for (const [path, what, disk, refuses] of [["/api/network", "the network", "", true],
      ["/api/vm-disks", "the imported disks", "win-disk", true],
      ["/api/vm-disks", "the imported disks", "", false], ["/api/namespaces", "the other namespaces", "", false]]) {
      await page.evaluate(() => { window.api = window.__realApi; });
      await page.evaluate(failing, path);
      await page.evaluate(disk => vmNew(disk, disk ? "lab" : ""), disk);
      await settled(page);
      assert.match(await body(), new RegExp(`Could not read ${what}[\\s\\S]*timed out`));
      assert.equal(await page.locator("#v_steps").count(), refuses ? 0 : 1, `${path} ${disk ? "with" : "without"} a chosen disk`);
      await close();
    }

    // Promoting the second container: a failed read leaves the form as it was,
    // not half switched to the other container's settings.
    await page.evaluate(() => { window.api = window.__realApi; });
    await page.evaluate(() => go("deploy"));
    await page.waitForSelector("#d_target_mode");
    const before = await page.evaluate(() => { deployAddContainer(); collect(); return JSON.stringify(DCFG); });
    await page.evaluate(failing, "/api/deploy/options");
    await page.evaluate(() => deployRemovePrimary());
    await page.waitForFunction(() => /Could not read the storage options/.test(document.querySelector("#toast")?.innerText || ""));
    assert.equal(await page.evaluate(() => JSON.stringify(DCFG)), before, "the form's settings are unchanged");

    // Addresses: an unread network is not "no addresses to choose from".
    const noNetwork = async () => {
      await page.evaluate(() => { window.api = window.__realApi; });
      await page.evaluate(failing, "/api/network");
    };
    await noNetwork();
    await page.evaluate(() => importSetup("unraid", "media-server"));
    await settled(page);
    assert.match(await body(), /Could not read the network[\s\S]*timed out/);
    assert.equal(await page.locator("#im_steps").count(), 0, "no import wizard without the network");
    await close();

    // With ServiceLB there is no address to choose, so the import does not need the network.
    const platform = await page.evaluate(() => JSON.stringify(STATE.platform || null));
    await noNetwork();
    await page.evaluate(() => { STATE.platform = { ...(STATE.platform || {}), load_balancer: "servicelb" };
      return importSetup("unraid", "media-server"); });
    await settled(page);
    assert.equal(await page.locator("#im_steps").count(), 1, "the import opens under ServiceLB");
    await close();
    await page.evaluate(platform => { STATE.platform = JSON.parse(platform); }, platform);

    await noNetwork();
    await page.evaluate(() => { STATE.platform = { ...(STATE.platform || {}), load_balancer: "kube-vip" };
      STATE.data.sambaInstalled = false; return sambaSet(true); });
    await settled(page);
    assert.match(await body(), /Could not read the network[\s\S]*timed out/);
    assert.equal(await page.locator("#mbody").getByRole("button", {name: "Install", exact: true}).count(), 0, "no Install for Samba");
    await close();

    await noNetwork();
    await page.evaluate(() => { STATE.platform = { ...(STATE.platform || {}), load_balancer: "kube-vip" };
      STATE.data.nfs = { installed: false, exports: [{ name: "media" }] }; return nfsSet(true); });
    await settled(page);
    assert.match(await body(), /Could not read the network[\s\S]*timed out/);
    assert.equal(await page.locator("#mbody").getByRole("button", {name: "Install NFS server"}).count(), 0);
    await close();

    // New share, when it would also install Samba: its address picker says so.
    await noNetwork();
    await page.evaluate(() => {
      const read = window.api;
      window.api = (path, ...rest) => String(path).split("?")[0] === "/api/shares/options"
        ? read(path, ...rest).then(options => ({ ...options, samba_installed: false })) : read(path, ...rest);
      return newShare();
    });
    await page.waitForFunction(() => document.querySelector("#sh_smb") && !document.querySelector("#sh_smb .spin2"));
    assert.match(await page.locator("#sh_smb").innerText(), /Could not read the network[\s\S]*timed out/);
    await close();
    await page.evaluate(platform => { STATE.platform = JSON.parse(platform); }, platform);
    await page.evaluate(() => { window.api = window.__realApi; });

    assert.deepEqual(errors, []);
    console.log(`Unread reads say so passed ${width}px`);
    await context.close();
  }
} finally {
  await browser.close();
}
