// Exercise the actual Manager 2.0 rendering/state/request code without a server.
const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
const html=fs.readFileSync(require('node:path').join(__dirname,'../luuvcrm/www/manager_v2.html'),'utf8');
const code=html.slice(html.indexOf('  const TONE='),html.indexOf('  // ── Month picker'));
const posts=[];
const ctx=vm.createContext({document:{getElementById:()=>null},icons(){},nfmt:n=>String(n),
  esc:s=>String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;'),
  toast(){},_smsg:()=>'',loadDashboard(){},pLoaded:false,
  _post:async(url,payload)=>{posts.push(payload);return {ok:true,json:async()=>({message:{ok:true}})};}});
vm.runInContext(code,ctx);
vm.runInContext(`
  renderApprovalFeed=function(){}; loadApprovals=function(){};
  const fixture={name:'review',type:'bill',status:'pending',mine:false,lines:[
    {row_name:'row1',name:'Sauce',qty:2,category:'Food / Ingredients',has_item:1},
    {row_name:'row2',name:'Sauce',qty:3,category:'Food / Ingredients',has_item:1}],
    line_category_options:[{value:'Food / Ingredients',has_item_list:1,in_stock:1},{value:'Packaging',has_item_list:0,in_stock:0}]};
  _apprView.all=[fixture];
`,ctx);
assert.match(vm.runInContext('renderApprovalCard(fixture)',ctx),/Category for Sauce/);
vm.runInContext("apprState.review.ticks={0:true,1:true}; apprLineCategory('review',0,'Packaging')",ctx);
assert.equal(vm.runInContext('apprState.review.ticks[0]',ctx),false);
assert.equal(vm.runInContext('apprState.review.ticks[1]',ctx),true);
const rerender=vm.runInContext('renderApprovalCard(fixture)',ctx);
assert.match(rerender,/value="Packaging" selected/);
assert.match(rerender,/This line will not add stock/);
assert.match(vm.runInContext('renderApprovalCard({...fixture,mine:true,self_ok:false})',ctx),/Category for Sauce" disabled/);
assert.doesNotMatch(vm.runInContext('renderApprovalCard({...fixture,status:"approved"})',ctx),/<select/);
(async()=>{
  await vm.runInContext("resolve('review','approved')",ctx);
  assert.deepEqual(JSON.parse(posts[0].line_categories),[{row_name:'row1',category:'Packaging'}]);
  assert.equal('category' in posts[0],false);
  vm.runInContext("renderApprovalCard(fixture);apprLineCategory('review',1,'Packaging')",ctx);
  await vm.runInContext("resolve('review','rejected')",ctx);
  assert.equal('line_categories' in posts[1],false);
  vm.runInContext("renderApprovalCard(fixture);apprLineCategory('review',1,'Packaging');apprLineCategory('review',1,'Food / Ingredients')",ctx);
  assert.equal(vm.runInContext('Object.keys(apprState.review.lineCategories).length',ctx),0);
  console.log('PASS actual approval UI: independent duplicate rows, polling state, review ticks, locked cards, approve/reject payloads');
})().catch(e=>{console.error(e);process.exitCode=1;});
