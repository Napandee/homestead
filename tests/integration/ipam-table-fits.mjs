// Networking › IP addresses fits its box at desktop widths (#559). Wide, its
// columns fit with no sideways scroll and every Seen chip inside the box.
// Narrower than the columns need, each address is a card, and the cards keep
// what the hidden headings gave: a Sort by menu and Select all.
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {mkdir} from 'node:fs/promises';
const base=process.env.HOMESTEAD_URL || 'http://127.0.0.1:4173',out=process.env.IPAM_FIT_SCREENSHOTS || 'release-assets/ipam-fit';
await mkdir(out,{recursive:true});const browser=await chromium.launch({headless:true});
try{for(const width of [1440,1366,1280,1100]){
 const page=await browser.newPage({viewport:{width,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`${base}/networking?tab=ip&demo=1`);await page.locator('.ipam-table tbody tr.ipam-row').first().waitFor();
 const fit=await page.locator('.ipam-table').evaluate(table=>{
  const box=table.closest('.tblwrap'),edge=box.getBoundingClientRect().right;
  const outside=[...table.querySelectorAll('.ip-seen .pill, .ip-seen .tag')]
   .filter(chip=>chip.getBoundingClientRect().right>edge+1).map(chip=>chip.textContent.trim());
  return {box:box.clientWidth,table:box.scrollWidth,outside,cards:getComputedStyle(table.querySelector('thead')).display==='none'};
 });
 assert.ok(fit.table<=fit.box+1,`at ${width}px the address table fits its box: ${fit.table}px in ${fit.box}px`);
 assert.deepEqual(fit.outside,[],`at ${width}px every Seen chip is inside the table`);
 if(fit.cards){
  assert.ok(await page.getByLabel('Sort by').isVisible(),`at ${width}px the cards have a Sort by menu`);
  const all=page.getByLabel('Select every address shown').filter({visible:true});
  assert.equal(await all.count(),1,`at ${width}px the cards have one visible Select all`);
  await all.check();
  assert.ok(await page.locator('#ipamBulk').isVisible(),'Select all picks the addresses');
  assert.ok(await page.locator('.ipam-pick:checked').count()>0);
  await all.uncheck();
  assert.equal(await page.locator('.ipam-pick:checked').count(),0,'and unticking it clears them');
 }else{
  assert.equal(await page.getByLabel('Sort by').isVisible(),false,`at ${width}px the headings sort, so no extra menu`);
 }
 await page.locator('.ipam-table').screenshot({path:`${out}/ipam-${width}.png`});
 assert.deepEqual(errors,[]);await page.close();console.log(`IP address table fits at ${width}px (${fit.cards?'cards':'columns'})`);
}}finally{await browser.close();}
