// Starting a StatefulSet's stuck pod on another host (#441), in the demo on a
// k3s cluster with one host down: the waiting set offers the button, the review
// keeps Start off until the set's name is typed, and the run follows its job.
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {mkdir} from 'node:fs/promises';
const base=process.env.HOMESTEAD_URL || 'http://127.0.0.1:4173',out=process.env.SET_RESTART_SCREENSHOTS || 'release-assets/set-restart';
await mkdir(out,{recursive:true});const browser=await chromium.launch({headless:true});
try{for(const width of [1440,390])for(const theme of ['dark','light']){
 const page=await browser.newPage({viewport:{width,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`${base}/?demo=1&demo-scenario=critical&platform=k3s`);
 await page.waitForFunction(()=>typeof nodeIfDown==='function'&&typeof ME!=='undefined'&&ME&&document.querySelector('#gate')?.classList.contains('hidden'));
 await page.evaluate(()=>go('nodes'));await page.waitForFunction(()=>STATE.data.nodes?.some(n=>n.status==='NotReady'));
 await page.evaluate(t=>{document.documentElement.dataset.theme=t;return nodeIfDown('harvester-node3');},theme);
 const button=page.getByRole('button',{name:'Start on another host…'});
 await button.waitFor();assert.equal(await button.count(),1,'the waiting set on the down host offers the button');
 await button.click();
 const run=page.locator('#sr_run');await run.waitFor();
 assert.equal(await run.isDisabled(),true,'Start is off until the name is typed');
 await page.locator('#sr_confirm').fill('wrong');assert.equal(await run.isDisabled(),true,'a wrong name keeps Start off');
 await page.locator('#sr_confirm').fill('postgres');assert.equal(await run.isDisabled(),false,'the right name turns Start on');
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'the page never scrolls sideways');
 await page.screenshot({path:`${out}/review-${width}-${theme}.png`});
 await run.click();
 await page.locator('#srProgress').waitFor();
 await page.waitForFunction(()=>/Starting|Waiting|Evicting|Removing|Deleted|pod/i.test(document.querySelector('#srProgress')?.textContent||''));
 await page.screenshot({path:`${out}/job-${width}-${theme}.png`});
 assert.deepEqual(errors,[]);await page.close();console.log(`Start on another host passed at ${width}px, ${theme}`);
}}finally{await browser.close();}
