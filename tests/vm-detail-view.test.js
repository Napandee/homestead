"use strict";
const test=require("node:test"), assert=require("node:assert/strict"), fs=require("node:fs"), vm=require("node:vm");
/* The VM's own dialog, as GET /api/vm answers it: the namespace is "ns". */
function open(detail) {
  const body={innerHTML:""};
  const ctx={console, URLSearchParams, Map, Date, Promise, encodeURIComponent,
    document:{addEventListener(){}},STATE:{data:{}},$:key=>key==="#mbody" ? body : null,$$:()=>[],
    esc:v=>String(v).replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll('"',"&quot;"),
    toast(){},refresh(){},modal(){},icon:()=>"",
    api:async path=>{assert.match(path,/^\/api\/vm\?/);return detail;}};
  ctx.window=ctx;vm.createContext(ctx);require("./helpers/load-ui")(ctx);
  ctx.jsArg=s=>JSON.stringify(String(s??""));ctx.jsq=s=>ctx.esc(ctx.jsArg(s));
  vm.runInContext(fs.readFileSync("web/js/views-vms.js","utf8"),ctx);
  return {ctx,body};
}
const detail={ns:"lab",name:"guest",status:"Running",run_strategy:"Always",node:"node-a",actions:[],
  cores:2,memory:"4Gi",disks:[],nics:[{name:"default",model:"virtio",network:"pod network",mac:"",ips:[]}],
  conditions:[],events:[]};
test("a VM's Configure VIP / ports button names the VM's namespace",async()=>{
  const t=open(detail);
  await t.ctx.vmOpen("lab","guest");
  const call=t.body.innerHTML.match(/onclick="(networkManage\([^"]*\))"/);
  assert.ok(call,"the Network tab offers Configure VIP / ports");
  const args=[];
  vm.runInContext(call[1].replaceAll("&quot;",'"'),Object.assign(t.ctx,{networkManage:(...a)=>args.push(...a)}));
  assert.deepEqual(args,["lab","guest","VirtualMachine"]);
});
