"use strict";
/* A Helm release and a Kubernetes resource open in the shared section tabs,
   as a VM's details do (#559): a rail on a desktop, a Section picker on a
   phone, and a Close footer. No hand-built .seg tab strip. */
const test=require("node:test"), assert=require("node:assert/strict"), fs=require("node:fs"), vm=require("node:vm");
function load(file, answer) {
  const body={innerHTML:""};
  const ctx={console, URLSearchParams, Map, Date, Promise, encodeURIComponent,
    document:{addEventListener(){}},STATE:{data:{}},$:key=>key==="#mbody" ? body : null,$$:()=>[],
    esc:v=>String(v).replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll('"',"&quot;"),
    toast(){},refresh(){},modal(){},icon:()=>"",tip:()=>"",fmtWhen:v=>String(v),fmtAgo:v=>String(v),
    appAvatar:()=>"",loadAppIcons(){},
    api:async path=>answer(path)};
  ctx.window=ctx;vm.createContext(ctx);require("./helpers/load-ui")(ctx);
  ctx.jsArg=s=>JSON.stringify(String(s??""));ctx.jsq=s=>ctx.esc(ctx.jsArg(s));
  vm.runInContext(fs.readFileSync(file,"utf8"),ctx);
  return {ctx,body};
}
const tabs=html=>[...html.matchAll(/role="tab"[^>]*>([^<]*)<\/button>/g)].map(m=>m[1].split(" ·")[0]);
const closes=html=>/<button[^>]*data-dialog-dismiss="true"[^>]*>Close<\/button>/.test(html);

const release={chart:"grafana",chart_version:"8.5.1",app_version:"11.2.0",description:"",status:"deployed",revision:3,
  source:{repo:"https://example.com/charts",chart:"grafana",version:"8.5.1",values:"replicas: 1\n"},
  values:"",notes:"Open it on port 3000.",history:[{revision:3,status:"deployed",chart_version:"8.5.1",updated:"2026-10-01T00:00:00Z",description:"Upgrade complete"}],
  objects:[{kind:"Deployment",name:"grafana",namespace:"monitoring"}]};
test("a Helm release opens in the shared section tabs with a Close footer",async()=>{
  const t=load("web/js/views-helm.js",path=>{assert.match(path,/^\/api\/helm\/release\?/);return release;});
  await t.ctx.helmRelease("monitoring","grafana");
  const html=t.body.innerHTML;
  assert.doesNotMatch(html,/class="seg"/,"no hand-built tab strip");
  assert.match(html,/role="tablist"/);
  assert.deepEqual(tabs(html),["Values","Notes","History","Objects"]);
  assert.ok(closes(html),"a Close footer");
  assert.equal(typeof t.ctx.helmTab,"undefined","the old tab switcher is gone");
  assert.match(html,/<span data-need="admin"><div class="actionbar"><details class="actionmenu"><summary class="btn sm" aria-label="More actions for grafana">/,
    "the header's ⋯ holds only Uninstall, so the whole menu is hidden from non-admins");
});

test("a Kubernetes resource opens in the shared section tabs with a Close footer",async()=>{
  const t=load("web/js/views-resources.js",path=>{
    if(path.startsWith("/api/resources/object"))return {yaml:"kind: ConfigMap\n",object:{metadata:{uid:"u1"}}};
    if(path.startsWith("/api/resources/events"))return [];
    throw new Error(path);
  });
  vm.runInContext('RES.pick={group:"",version:"v1",resource:"configmaps",kind:"ConfigMap"}',t.ctx);
  await t.ctx.resOpen("lab","settings");
  const html=t.body.innerHTML;
  assert.doesNotMatch(html,/class="seg"/,"no hand-built tab strip");
  assert.match(html,/role="tablist"/);
  assert.deepEqual(tabs(html),["YAML","Events"]);
  assert.ok(closes(html),"a Close footer");
  assert.equal(typeof t.ctx.resTab,"undefined","the old tab switcher is gone");
  assert.match(html,/<span data-need="admin"><div class="actionbar"><details class="actionmenu"><summary class="btn sm" aria-label="More actions for settings">/,
    "the header's ⋯ holds only Delete, so the whole menu is hidden from non-admins");
});

test("the hand-built panes are gone",()=>{
  for(const file of ["web/js/views-helm.js","web/js/views-resources.js"])
    assert.doesNotMatch(fs.readFileSync(file,"utf8"),/helm-pane|res-pane/,file);
});
