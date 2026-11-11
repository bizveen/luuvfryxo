// Existing production templates, isolated local data, two explicit preview roles.
const {chromium,request}=require('playwright');
const {spawn}=require('node:child_process');
const path=require('node:path'),fs=require('node:fs'),assert=require('node:assert/strict');
(async()=>{
  const dir=path.join(__dirname,'data');fs.mkdirSync(dir,{recursive:true});
  const server=spawn('python',[path.join(__dirname,'server.py'),'--port','4196','--db',path.join(dir,'existing-apps-'+Date.now()+'.sqlite3')],{windowsHide:true,stdio:['ignore','pipe','pipe']});
  let browser,api;let stderr='';server.stderr.on('data',d=>stderr+=d);
  try{
    await new Promise((resolve,reject)=>{server.stdout.once('data',resolve);server.once('error',reject);server.once('exit',c=>reject(Error('server '+c+' '+stderr)));setTimeout(()=>reject(Error('server timeout '+stderr)),10000).unref()});
    const base='http://127.0.0.1:4196';api=await request.newContext();
    const call=async(method,form={},client=api)=>{const r=await client.post(base+'/api/method/luuvcrm.api.'+method,{form});const d=await r.json();assert(r.ok(),JSON.stringify(d));return d.message};
    const inv=(method,form)=>call('manager_inventory.'+method,form);
    const balance=async item=>(await inv('dashboard')).stock.find(r=>r.item===item).balance;
    await inv('demo_stock');await inv('correct_stock',{item_code:'KG-STOCK-HOT',expected_qty:10,counted_qty:0,comment:'Test low stock'});
    await inv('set_minimum',{item_code:'KG-STOCK-HOT',minimum_qty:2});
    await inv('set_minimum',{item_code:'KG-STOCK-SALAD',minimum_qty:20});
    browser=await chromium.launch({headless:true});
    const managers=await browser.newContext({viewport:{width:1440,height:1100}}),purchasers=await browser.newContext({viewport:{width:1200,height:1000}});
    const manager=await managers.newPage(),purchases=await purchasers.newPage();const errors=[];
    for(const page of [manager,purchases])page.on('pageerror',e=>errors.push(e.message));
    await manager.goto(base+'/manager2.0?tab=inventory');await manager.locator('[data-stock="KG-STOCK-HOT"]').waitFor();
    const link=await manager.getByRole('link',{name:'Open Purchasing'}).getAttribute('href');assert.match(link,/restaurant=Kurunegala/);
    await purchases.goto(base+link);await purchases.locator('[data-restock="KG-STOCK-HOT"]').waitFor();
    assert.equal(await purchases.locator('[data-restock="KG-STOCK-SALAD"]').count(),0);
    assert.match(await purchases.locator('#purchaseStock').innerText(),/Prepare in branch: Prepared salad/);
    assert.equal(await purchases.locator('#psCategory option[value="Packaging"]').count(),0);
    await purchases.locator('#psCategory').selectOption('Food / Ingredients');
    await purchases.getByLabel('Select Hot sauce',{exact:true}).check();
    await purchases.getByLabel('Bill quantity for Hot sauce',{exact:true}).fill('3');
    await purchases.getByRole('button',{name:'Add selected to bill',exact:true}).click();
    await purchases.locator('#addOverlay').waitFor({state:'visible'});
    await purchases.locator('#fVendor').fill('Sample Central Kitchen');await purchases.locator('#fNote').fill('Local integration test');
    const price=purchases.locator('#addOverlay input[oninput^="setPrice("]').first();await price.fill('1500');await price.press('Tab');
    // Navigation restores the existing bill, and handoff must not overwrite it.
    await purchases.reload();await purchases.locator('#addOverlay').waitFor({state:'visible'});
    assert.equal(await purchases.locator('#fVendor').inputValue(),'Sample Central Kitchen');
    assert.equal(await purchases.locator('#addOverlay input[oninput^="setQty("]').first().inputValue(),'3');
    await purchases.locator('#psCategory').selectOption('Food / Ingredients');
    await purchases.getByLabel('Select Hot sauce',{exact:true}).check();
    await purchases.getByLabel('Bill quantity for Hot sauce',{exact:true}).fill('99');
    await purchases.getByRole('button',{name:'Add selected to bill',exact:true}).click();
    await purchases.getByText(/already in this bill; existing quantities kept/).first().waitFor();
    assert.equal(await purchases.locator('#fVendor').inputValue(),'Sample Central Kitchen');
    assert.equal(await purchases.locator('#addOverlay input[oninput^="setQty("]').first().inputValue(),'3');
    assert.equal(await purchases.locator('#addOverlay input[oninput^="setPrice("]').first().inputValue(),'1500');
    await purchases.locator('#submitBtn').click();await purchases.locator('#addOverlay').waitFor({state:'hidden'});
    assert.equal(await balance('KG-STOCK-HOT'),0);
    await manager.waitForFunction(()=>document.querySelector('[data-reorder="KG-STOCK-HOT"]')?.textContent.includes('Awaiting approval: 3 L'));
    await purchases.getByRole('button',{name:'Refresh stock',exact:true}).click();
    await purchases.waitForFunction(()=>document.querySelector('[data-restock="KG-STOCK-HOT"]')?.textContent.includes('Awaiting approval: 3 L'));
    const own=await call('get_purchase_approvals',{},purchasers.request);assert.equal(own.approvals[0].mine,true);
    const bill=own.approvals[0].name;
    const denied=await purchasers.request.post(base+'/api/method/luuvcrm.api.resolve_approval',{form:{name:bill,decision:'approved'}});assert.equal(denied.status(),400);
    await manager.getByRole('button',{name:'Approvals',exact:true}).click();
    await manager.locator('#approvalFeed input[type=checkbox]').first().check();
    await manager.locator('#approvalFeed').getByRole('button',{name:'Approve',exact:true}).click();
    await manager.waitForFunction(()=>!document.querySelector('#approvalFeed button[id^="approve_"]'));
    await manager.waitForTimeout(400);assert.equal(await balance('KG-STOCK-HOT'),3);
    const duplicate=await call('resolve_approval',{name:bill,decision:'approved'},managers.request);assert.equal(duplicate.already_processed,true);assert.equal(await balance('KG-STOCK-HOT'),3);
    await manager.goto(base+'/manager2.0?tab=inventory');await manager.locator('[data-stock="KG-STOCK-HOT"] .mi-stock-amount strong').getByText('3',{exact:true}).waitFor();
    assert.equal(await manager.locator('[data-reorder="KG-STOCK-HOT"]').count(),0);
    await manager.screenshot({path:path.join(dir,'existing-manager-desktop.png'),fullPage:true});
    await purchases.reload();await purchases.locator('#purchaseStock h2').waitFor();
    assert.equal(await purchases.locator('[data-restock="KG-STOCK-HOT"]').count(),0);
    await purchases.screenshot({path:path.join(dir,'existing-purchases-desktop.png'),fullPage:true});
    // Omit / replace still uses the same shared local stock after the app change.
    const recipe=await inv('recipe');await inv('activate_recipe',{version:recipe.version});
    const created=await inv('create_order',{qty:1,version:recipe.version,customizations:JSON.stringify([{item:'KG-STOCK-HOT',action:'omit'}])});
    await inv('fire_order',{order:created.name});assert.equal(await balance('KG-STOCK-HOT'),3);
    // Failed branch handoff never adds another draft or allows a cross-branch read.
    await purchases.goto(base+'/purchase2.0?inventory=1&restaurant=Other');
    await purchases.getByText('Switch to that branch before managing its stock.',{exact:true}).waitFor();
    for(const page of [manager,purchases]){await page.setViewportSize({width:390,height:844});await page.goto(base+(page===manager?'/manager2.0?tab=inventory':'/purchase2.0?inventory=1&restaurant=Kurunegala'));await page.waitForTimeout(500);assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));}
    await manager.screenshot({path:path.join(dir,'existing-manager-mobile.png'),fullPage:true});
    await purchases.screenshot({path:path.join(dir,'existing-purchases-mobile.png'),fullPage:true});
    assert.deepEqual(errors,[]);console.log('PASS: native manager/purchases templates; low-stock handoff; draft restore; role checks; pending bill; approval; one receipt; shared deductions; branch guard; mobile layouts.');
  }finally{await api?.dispose();await browser?.close();server.kill();}
})().catch(e=>{console.error(e);process.exitCode=1});
