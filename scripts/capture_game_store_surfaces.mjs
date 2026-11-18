/** Visual inventory of current public routes. No authentication or payment writes. */
import { createRequire } from 'node:module';
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
const require = createRequire(import.meta.url);
const {chromium} = require(process.env.AURIX_PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.AURIX_GAME_UI_URL || 'http://127.0.0.1:64100';
assert(['localhost','127.0.0.1'].includes(new URL(base).hostname));
const out = process.env.AURIX_GAME_UI_OUTPUT || '.local/screen-refinement-20261111';
await mkdir(out,{recursive:true});
const browser=await chromium.launch({channel:'chrome',headless:true});
const records=[];
try {
 for(const width of [320,390,1440]){
  const page=await browser.newPage({viewport:{width,height:900},serviceWorkers:'block'});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(base+'/game-store/');await page.locator('.product-card').first().waitFor();
  const nav=await (await page.request.get(base+'/api/v1/navigation')).json();
  async function capture(name){
   await page.evaluate(()=>Promise.all([...document.querySelectorAll('dialog[open]')].flatMap(el=>el.getAnimations()).map(a=>a.finished.catch(()=>{}))));
   const geometry=await page.evaluate(()=>{
    const el=document.querySelector('dialog[open]') || document.documentElement;
    const body=el.querySelector('.auxiliary-dialog__body,.product-detail__body,.wallet-dialog__body') || el;
    return {document:document.documentElement.scrollWidth,viewport:innerWidth,scrollWidth:body.scrollWidth,clientWidth:body.clientWidth};
   });
   assert(geometry.document<=width, name+' document overflow');
   assert(geometry.scrollWidth<=geometry.clientWidth+1,name+' surface overflow: '+JSON.stringify(geometry));
   await page.evaluate(() => Promise.race([new Promise(resolve => setTimeout(resolve, 1500)), Promise.all([...document.images].filter(image => image.getBoundingClientRect().height > 0 && image.getBoundingClientRect().top < innerHeight && image.getBoundingClientRect().bottom > 0).map(image => image.decode().catch(() => {})))]));
   await page.screenshot({path:`${out}/${name}-${width}.png`});
   records.push({name,width,...geometry});
  }
  await capture('catalog');
  await page.locator('#nav-open').click();await capture('menu');await page.keyboard.press('Escape');
  for(const item of nav.items){
   if(item.id==='home')continue;
   await page.evaluate(id=>openNavigationById(id),item.id);
   await page.waitForFunction(()=>!document.querySelector('#auxiliary-status')?.textContent.startsWith('Loading'));
   await capture(item.id);
   await page.evaluate(()=>document.querySelectorAll('dialog[open]').forEach(el=>el.close()));
  }
  await page.evaluate(()=>openNavigationById('mbb-hero-guide'));
  await page.locator('.guide-tool-card').first().waitFor();
  const names=await page.locator('.guide-tool-card strong').allTextContents();
  for(const name of names){
   await page.locator('.guide-tool-card').filter({hasText:name}).first().click();
   await capture('tool-'+name.toLowerCase().replaceAll(/[^a-z0-9]+/g,'-'));
   if(name==='Hero Tiers'){
    const hero=page.locator('.guide-hero-card').first();if(await hero.count()){await hero.click();await capture('hero-detail');await page.locator('#auxiliary-close').click();}
   }
   await page.locator('#auxiliary-close').click();
  }
  const wikiNames=await page.locator('.guide-wiki-copy strong').allTextContents();
  for(const name of wikiNames){
   await page.locator('.guide-wiki-row').filter({hasText:name}).first().click();
   await capture('wiki-'+name.toLowerCase().replaceAll(/[^a-z0-9]+/g,'-'));
   const hero=page.locator('.guide-hero-card').first();
   if(await hero.count()){await hero.click();await capture('hero-detail');await page.locator('#auxiliary-close').click();}
   await page.locator('#auxiliary-close').click();
  }
  assert.deepEqual(errors,[]);await page.close();
 }
 await writeFile(out+'/surface-inventory.json',JSON.stringify(records,null,2));
 console.log(`Captured and checked ${records.length} route/width combinations.`);
}finally{await browser.close()}
