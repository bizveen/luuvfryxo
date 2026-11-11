const {chromium,request}=require('playwright');
const {spawn}=require('node:child_process');
const path=require('node:path');
const fs=require('node:fs');
const assert=require('node:assert/strict');

(async()=>{
  const dir=path.join(__dirname,'data');fs.mkdirSync(dir,{recursive:true});
  const server=spawn('python',[path.join(__dirname,'server.py'),'--port','4193','--db',path.join(dir,'realtime-'+Date.now()+'.sqlite3')],{windowsHide:true,stdio:['ignore','pipe','pipe']});
  let browser,api;
  try{
    await new Promise((resolve,reject)=>{server.stdout.once('data',resolve);server.once('error',reject);server.once('exit',c=>reject(Error('server '+c)));setTimeout(()=>reject(Error('server timeout')),10000).unref()});
    api=await request.newContext();
    const call=async(method,form={})=>{const r=await api.post('http://127.0.0.1:4193/api/method/luuvcrm.api.manager_inventory.'+method,{form});const d=await r.json();assert(r.ok(),JSON.stringify(d));return d.message};
    await call('demo_stock');const recipe=await call('recipe');await call('activate_recipe',{item_code:recipe.item,version:recipe.version});
    await call('set_minimum',{item_code:'KG-STOCK-POTATO',minimum_qty:9.9});
    browser=await chromium.launch({headless:true});const context=await browser.newContext({viewport:{width:1440,height:1000}});
    const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto('http://127.0.0.1:4193/manager2.0');await page.locator('.mi-live-status.connected').first().waitFor();
    assert.equal(await page.locator('[data-reorder="KG-STOCK-POTATO"]').count(),0);
    const order=await call('create_order',{qty:1}),start=Date.now();
    await call('fire_order',{order:order.name});
    await page.waitForFunction(()=>document.querySelector('[data-stock="KG-STOCK-POTATO"] .mi-stock-amount strong')?.textContent==='9.85',{},{timeout:2000});
    const latency=Date.now()-start;
    assert.equal(await page.locator('[data-reorder="KG-STOCK-POTATO"]').count(),1);
    assert.match(await page.locator('[data-stock="KG-STOCK-POTATO"] .mi-stock-delta').textContent(),/-0.15 Kg/);
    await page.getByRole('button',{name:'Graphs only',exact:false}).click();
    assert.equal(await page.locator('[data-audit]').count(),0);
    assert.equal(await page.locator('[data-minimum]').count(),0);
    assert.equal(await page.locator('#miOrderSummary').isVisible(),false);
    const order2=await call('create_order',{qty:2});await call('fire_order',{order:order2.name});
    await page.waitForFunction(()=>document.querySelector('[data-stock="KG-STOCK-POTATO"] .mi-stock-amount strong')?.textContent==='9.55',{},{timeout:2000});
    await page.screenshot({path:path.join(dir,'realtime-graphs.png'),fullPage:true});
    await context.setOffline(true);
    const order3=await call('create_order',{qty:1});await call('fire_order',{order:order3.name});
    await context.setOffline(false);
    await page.waitForFunction(()=>document.querySelector('[data-stock="KG-STOCK-POTATO"] .mi-stock-amount strong')?.textContent==='9.4',{},{timeout:6000});
    await page.locator('.mi-live-status.connected').last().waitFor();
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:path.join(dir,'realtime-graphs-mobile.png'),fullPage:true});
    await page.getByRole('button',{name:'Piles',exact:false}).click();
    assert.equal(await page.locator('#miOrderSummary').isVisible(),true);
    assert.equal(await page.locator('[data-reorder="KG-STOCK-POTATO"]').count(),1);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({result:'PASS',push_update_ms:latency,checks:['committed event push','automatic order summary','quantity decrease badge','graphs-only controls hidden','graphs-only live update','disconnect/reconnect catch-up','mobile layout']}));
  }finally{if(browser)await browser.close();if(api)await api.dispose();server.kill()}
})().catch(e=>{console.error(e);process.exitCode=1});
