import { createRequire } from 'node:module';
import assert from 'node:assert/strict';
import { mkdir } from 'node:fs/promises';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.AURIX_PLAYWRIGHT_MODULE || 'playwright');
const output = process.env.AURIX_GAME_UI_OUTPUT || '.local/ui-interaction-20261111';
await mkdir(output, { recursive: true });
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 try {
  for(const width of [320,390,768,1440]){
   const page=await browser.newPage({viewport:{width,height:844},serviceWorkers:'block'});
   await page.goto('http://127.0.0.1:64100/game-store/');
   await page.locator('.product-card').first().waitFor();
   const button=page.locator('#nav-open'), b=await button.boundingBox();
   await page.mouse.move(b.x+b.width/2,b.y+b.height/2);await page.mouse.down();
   assert.equal(await button.getAttribute('data-press'),'down');
   assert.deepEqual(await button.boundingBox(),b);
   await page.mouse.move(b.x-30,b.y+70);await page.mouse.up();
   assert.equal(await page.locator('#navigation-dialog').evaluate(x=>x.open),false);
   await button.click();await page.locator('#navigation-dialog').evaluate(el=>Promise.all(el.getAnimations().map(a=>a.finished)));
   const drawer=await page.locator('#navigation-dialog').boundingBox();assert.equal(drawer.x,0);assert.equal(drawer.y,0);
   assert(await page.locator('.navigation-item__icon').count()>0);
   await page.screenshot({path:output+'/menu-'+width+'.png'});
   await page.keyboard.press('Escape');
   if(width<761){
    const wallet=page.locator('#floating-wallet'), box=await wallet.boundingBox();
    const icon=await page.locator('.floating-wallet__symbol svg').boundingBox();
    assert(icon.x>=box.x && icon.y>=box.y && icon.x+icon.width<=box.x+box.width && icon.y+icon.height<=box.y+box.height);
    await wallet.click();await page.waitForTimeout(220);
    assert(await page.locator('#wallet-dialog').evaluate(x=>x.open));
    await page.keyboard.press('Escape');
   }
   await page.screenshot({path:output+'/intent-home-'+width+'.png'});
   await page.close();
  }
  // Touch scrolling cancels a pending action rather than entering detail.
  const touch=await browser.newPage({viewport:{width:390,height:844},hasTouch:true,isMobile:true,serviceWorkers:'block'});
  await touch.goto('http://127.0.0.1:64100/game-store/');
  const c=touch.locator('.category-chip').first();await c.waitFor();
  await c.dispatchEvent('pointerdown',{pointerId:9,pointerType:'touch',button:0,clientX:30,clientY:200});
  await c.dispatchEvent('pointercancel',{pointerId:9,pointerType:'touch'});
  assert.equal(await c.getAttribute('data-press'),'cancelled');
  await touch.close(); console.log('Intent, cancellation, drawer and wallet bounds passed at four widths.');
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
