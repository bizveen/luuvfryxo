const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../luuvcrm/templates/includes/purchase_category_correction.html'),'utf8').split('<script>')[1].split('</script>')[0];
const elements={},selects=[{},{}],posts=[];
const data={revision:'v1',warehouse:'Test stock',history:[],balances:{RAW:5},
  categories:[{value:'Food',label:'Food',in_stock:true},{value:'Expense',label:'Expense',in_stock:false}],
  bill:{name:'bill',vendor:'Test bill',total:500,lines:[
    {row_name:'first',item:'RAW',name:'Ingredient',qty:2,price:100,unit:'Kg',stock_unit:'Kg',stock_qty:2,received_qty:2,category:'Food',is_stock:true,can_change:true,can_receive:true},
    {row_name:'second',item:'RAW',name:'Ingredient',qty:3,price:100,unit:'Kg',stock_unit:'Kg',stock_qty:3,received_qty:3,category:'Food',is_stock:true,can_change:true,can_receive:true}]} };
let attempt=0;
const ctx=vm.createContext({window:{crypto:null},Date,Math,document:{getElementById:id=>elements[id]||(elements[id]={style:{},value:''}),querySelectorAll:()=>selects},
  esc:s=>String(s??''),toast(){},_post:async(url,body)=>{
    posts.push({url,body});
    if(url.endsWith('correct')&&attempt++===0)throw new Error('lost response');
    return {ok:true,json:async()=>({message:url.endsWith('review')?data:{ok:true,bill:data.bill}})};
  }});
vm.runInContext(source,ctx);
Object.assign(ctx,ctx.window);
(async()=>{
  await ctx.openPurchaseCategories(data.bill,()=>{});
  assert.match(elements.pccLines.innerHTML,/Category for Ingredient/);
  ctx.purchaseCategoryChoice(0,'Expense');
  assert.match(elements.pccStock.innerHTML,/-2 Kg.*balance 3/);
  assert.equal(elements.pccSave.disabled,false);
  await ctx.savePurchaseCategories();
  assert.match(elements.pccError.textContent,/Retry/);
  await ctx.savePurchaseCategories();
  const requests=posts.filter(p=>p.url.endsWith('correct')).map(p=>p.body);
  assert.equal(requests.length,2);
  assert.equal(requests[0].request_id,requests[1].request_id);
  assert.deepEqual(JSON.parse(requests[0].line_categories),[{row_name:'first',category:'Expense'}]);
  assert.equal(requests[0].expected_revision,'v1');
  assert.equal('qty' in requests[0],false);assert.equal('price' in requests[0],false);
  assert.equal(elements.pccOverlay.style.display,'none');
  console.log('PASS approved correction UI: per-row controls, stock preview, category-only payload and retry identity');
})().catch(error=>{console.error(error);process.exitCode=1;});
