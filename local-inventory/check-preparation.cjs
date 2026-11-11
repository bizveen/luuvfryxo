// Isolated UI regression. Does not modify the manager review database.
const {chromium,request}=require('playwright');
const {spawn}=require('node:child_process');
const path=require('node:path'),fs=require('node:fs'),assert=require('node:assert/strict');
(async()=>{
  const dir=path.join(__dirname,'data');fs.mkdirSync(dir,{recursive:true});
  const server=spawn('python',[path.join(__dirname,'server.py'),'--port','4194','--db',path.join(dir,'preparation-test-'+Date.now()+'.sqlite3')],{windowsHide:true,stdio:['ignore','pipe','pipe']});
  let browser,api;
  try{
    await new Promise((resolve,reject)=>{server.stdout.once('data',resolve);server.once('error',reject);server.once('exit',c=>reject(Error('server '+c)));setTimeout(()=>reject(Error('server timeout')),10000).unref()});
    api=await request.newContext();const base='http://127.0.0.1:4194';
    const call=async(method,form={})=>{const r=await api.post(base+'/api/method/luuvcrm.api.manager_inventory.'+method,{form});const d=await r.json();assert(r.ok(),JSON.stringify(d));return d.message};
    await call('demo_stock');const menu=await call('recipe');await call('activate_recipe',{version:menu.version});
    const raw=[];
    for(const [name,qty] of [['Cabbage',650],['Carrot',250],['Onion',100]]){
      const ingredient=await call('create_ingredient',{item_name:name,uom:'Kg'});
      await call('correct_stock',{item_code:ingredient.item,expected_qty:0,counted_qty:5,valuation_rate:200,comment:'Isolated sample opening'});
      raw.push({...ingredient,qty});
    }
    browser=await chromium.launch({headless:true});const context=await browser.newContext({viewport:{width:1440,height:1100}});
    const page=await context.newPage(),stock=await context.newPage(),errors=[];
    page.on('pageerror',e=>errors.push(e.message));page.on('dialog',d=>d.accept());stock.on('pageerror',e=>errors.push(e.message));
    await stock.goto(base+'/manager2.0?view=graphs');await stock.locator('.mi-live-status.connected').last().waitFor();
    await page.goto(base+'/manager2.0');await page.getByRole('button',{name:'Recipes',exact:true}).click();
    await page.locator('[data-item="KG-STOCK-SALAD"]').click();
    await page.locator('#miOutputQty').waitFor();assert.equal(await page.locator('#miOutputQty').inputValue(),'1000');
    assert.equal(await page.locator('[data-row]').count(),0);
    for(const [index,r] of raw.entries()){
      await page.getByRole('button',{name:'+ Add ingredient',exact:true}).click();
      await page.locator('[data-row="'+index+'"]').selectOption(r.item);
      await page.locator('[data-qty="'+index+'"]').fill(String(r.qty));
    }
    await page.locator('#miRecipeNote').fill('Sample preparation recipe for UI testing');
    await page.getByRole('button',{name:'Save new draft',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#miVersion').value!=='');
    const version=await page.locator('#miVersion').inputValue();
    await page.getByRole('button',{name:'Activate recipe',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('#miPrepare').disabled);
    await page.locator('#miPrepare').click();await page.locator('#miBatchPlanned').fill('2000');
    assert.equal(await page.locator('#miBatchActual').inputValue(),'2000');
    assert.match(await page.locator('#miBatchUsage').innerText(),/1,300 g/);
    await page.locator('#miBatchActual').fill('1900');await page.locator('#miBatchComplete').click();
    await page.getByText('Add a note for the difference in finished quantity.',{exact:true}).waitFor();
    await page.locator('#miBatchComment').fill('Sample trimming yield');await page.locator('#miBatchComplete').click();
    await page.locator('#miAudit').waitFor({state:'hidden'});await page.getByText('Batch completed.',{exact:false}).waitFor();
    await stock.waitForFunction(()=>document.querySelector('[data-stock="KG-STOCK-SALAD"] .mi-stock-amount strong')?.textContent==='11.9');
    await page.locator('#miBatches summary').click();assert.match(await page.locator('#miBatches').innerText(),/Cabbage · 1.3 Kg used/);
    const afterPrep=await call('dashboard');
    const order=await call('create_order',{qty:1});assert.equal(order.version,menu.version);await call('fire_order',{order:order.name});
    await stock.waitForFunction(()=>document.querySelector('[data-stock="KG-STOCK-SALAD"] .mi-stock-amount strong')?.textContent==='11.85');
    const afterSale=await call('dashboard');for(const r of raw)assert.equal(afterSale.stock.find(i=>i.item===r.item).balance,afterPrep.stock.find(i=>i.item===r.item).balance);
    await page.locator('#miOutputQty').fill('900');await page.locator('[data-qty="0"]').fill('600');
    await page.getByRole('button',{name:'Save new draft',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('#miVersion option').length===2);
    await page.locator('#miVersion').selectOption(version);await page.waitForFunction(()=>document.querySelector('#miOutputQty').disabled);
    assert.equal(await page.locator('#miOutputQty').inputValue(),'1000');assert.equal(await page.locator('[data-qty="0"]').inputValue(),'650');
    assert.match(await page.locator('#miBatches').innerText(),/1.9 Kg/);
    await page.screenshot({path:path.join(dir,'preparation-history-desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:path.join(dir,'preparation-recipe-mobile.png'),fullPage:true});
    await page.getByRole('button',{name:'+ Prepared item recipe',exact:true}).click();
    await page.getByRole('button',{name:'+ New finished item',exact:true}).click();
    await page.locator('#miIngredientName').fill('Test salad cups');await page.locator('#miIngredientUnit').selectOption('nos');
    await page.getByRole('button',{name:'Create ingredient',exact:true}).click();await page.locator('#miAudit').waitFor({state:'hidden'});
    assert.equal(await page.locator('#miOutputQty').inputValue(),'1');
    await page.getByRole('button',{name:'+ Add ingredient',exact:true}).click();await page.locator('[data-row="0"]').selectOption('KG-STOCK-SALAD');await page.locator('[data-qty="0"]').fill('100');
    await page.getByRole('button',{name:'Save new draft',exact:true}).click();await page.waitForFunction(()=>document.querySelector('#miVersion').value!=='');
    await page.getByRole('button',{name:'Activate recipe',exact:true}).click();await page.waitForFunction(()=>!document.querySelector('#miPrepare').disabled);
    await page.locator('#miPrepare').click();assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:path.join(dir,'preparation-dialog-mobile.png'),fullPage:true});
    await page.getByRole('button',{name:'Cancel',exact:true}).click();
    await page.getByRole('button',{name:'Stock on hand',exact:true}).click();
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({result:'PASS',checks:['prepared recipe creation','ingredient scaling','measured yield and reason','batch receipt','live stock push','sale uses prepared stock only','immutable recipe/batch history','new finished item','prepared item used in another recipe','desktop/mobile layouts']}));
  }finally{if(browser)await browser.close();if(api)await api.dispose();server.kill()}
})().catch(e=>{console.error(e);process.exitCode=1});
