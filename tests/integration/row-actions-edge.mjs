// A table's row actions end at its right edge, on every row, rather than at the
// left of a wide last column (#559). The ISO library dialog showed it worst:
// Remove, ⋯ and Make ready sat at three different places across the column.
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {mkdir} from 'node:fs/promises';
const base=process.env.HOMESTEAD_URL || 'http://127.0.0.1:4173',out=process.env.ROW_ACTIONS_SCREENSHOTS || 'release-assets/row-actions';
await mkdir(out,{recursive:true});const browser=await chromium.launch({headless:true});
try{
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`${base}/vms?demo=1`);await page.waitForFunction(()=>typeof window.vmIsoLibrary==='function');
 await page.evaluate(()=>window.vmIsoLibrary());
 const cells=page.locator('.modal td[data-actions]');await cells.first().waitFor();
 const rows=await cells.evaluateAll(tds=>tds.map(td=>{
  // The bar itself fills its cell, so measure its buttons: the last one's right edge.
  const bar=td.querySelector('.actionbar'),table=td.closest('table');
  const pad=parseFloat(getComputedStyle(td).paddingRight)||0;
  const right=Math.max(...[...bar.children].map(el=>el.getBoundingClientRect().right));
  return {bar:right,edge:table.getBoundingClientRect().right-pad,
   text:td.closest('tr').textContent.trim().slice(0,40)};
 }));
 assert.ok(rows.length>=3,`the ISO library shows several rows with actions (${rows.length})`);
 const ends=rows.map(r=>r.bar);
 assert.ok(Math.max(...ends)-Math.min(...ends)<=2,`every row's actions end at the same place (${ends.map(Math.round).join(', ')})`);
 for(const r of rows) assert.ok(Math.abs(r.bar-r.edge)<=2,`"${r.text}": its actions end at the table's right edge (${r.bar} vs ${r.edge})`);
 await page.locator('.modal').first().screenshot({path:`${out}/iso-library-1440.png`});
 assert.deepEqual(errors,[]);await page.close();console.log(`Row actions end at the table's right edge on ${rows.length} rows`);
}finally{await browser.close();}
