// Explicit user-requested sample orders in the local review DB only.
const {chromium,request}=require('playwright');
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
(async()=>{
  const base='http://127.0.0.1:4192',api=await request.newContext();let browser;
  const call=async(method,form={})=>{const r=await api.post(base+'/api/method/luuvcrm.api.manager_inventory.'+method,{form});const d=await r.json();assert(r.ok(),JSON.stringify(d));return d.message};
  try{
    const before=await call('dashboard'),existingOrders=await call('orders');
    const balances=Object.fromEntries(before.stock.map(r=>[r.item,r.balance]));
    const used={},orders=[];
    browser=await chromium.launch({headless:true});const page=await browser.newPage({viewport:{width:1440,height:1100}});
    await page.goto(base+'/manager');await page.locator('.mi-live-status.connected').first().waitFor();
    for(const qty of [1,2,1]){
      const order=await call('create_order',{qty});const recipe=await call('recipe',{item_code:'KG-FRIES-CHICKEN',version:order.version});
      const catalog=Object.fromEntries(recipe.catalog.map(r=>[r.item,r]));
      const start=Date.now(),receipt=await call('fire_order',{order:order.name});
      for(const row of recipe.ingredients){
        const expected=row.qty*qty/catalog[row.item].factor;
        const movement=receipt.ingredients.find(i=>i.item===row.item);
        assert(movement,'Missing deduction '+row.item);assert(Math.abs(movement.qty-expected)<1e-6,'Incorrect deduction '+row.item);
        used[row.item]=(used[row.item]||0)+expected;balances[row.item]=Math.round((balances[row.item]-expected)*1e6)/1e6;
      }
      await page.waitForFunction(expected=>Object.entries(expected).every(([item,quantity])=>{
        const card=[...document.querySelectorAll('[data-stock]')].find(e=>e.dataset.stock===item);
        return card&&Math.abs(Number(card.querySelector('.mi-stock-amount strong').textContent.replace(/,/g,''))-quantity)<1e-6;
      }),balances,{timeout:2000});
      orders.push({...order,screen_update_ms:Date.now()-start});
    }
    const after=await call('dashboard');
    for(const row of after.stock)assert(Math.abs(row.balance-balances[row.item])<1e-6,'Final balance mismatch '+row.item);
    const ordersAfter=await call('orders');
    for(const old of existingOrders)assert.deepEqual(ordersAfter.find(r=>r.name===old.name),old,'An existing order was changed');
    const duplicate=await call('fire_order',{order:orders.at(-1).name});assert.equal(duplicate.already_processed,true);
    assert.deepEqual((await call('dashboard')).stock.map(r=>r.balance),after.stock.map(r=>r.balance));
    const low=after.stock.filter(r=>r.minimum_qty>0&&r.balance<=r.minimum_qty);
    assert.equal(await page.locator('[data-reorder]').count(),low.length);
    const report={local_only:true,orders,portions:4,preserved_existing_orders:existingOrders.length,
      stocks:before.stock.map(r=>({item:r.name,unit:r.stock_uom,before:r.balance,deducted:Math.round((used[r.item]||0)*1e6)/1e6,after:balances[r.item]})),
      low_stock_count:low.length,duplicate_send_deducted:false};
    const dir=path.join(__dirname,'data');fs.writeFileSync(path.join(dir,'sample-orders-verification.json'),JSON.stringify(report,null,2));
    await page.screenshot({path:path.join(dir,'live-stock-summary.png'),fullPage:true});
    await page.getByRole('button',{name:'Graphs only',exact:false}).click();
    await page.screenshot({path:path.join(dir,'live-graphs-preview.png'),fullPage:true});
    console.log(JSON.stringify(report,null,2));
  }finally{if(browser)await browser.close();await api.dispose()}
})().catch(e=>{console.error(e);process.exitCode=1});
