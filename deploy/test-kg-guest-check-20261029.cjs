const fs=require('fs'),vm=require('vm'),assert=require('assert');
const s=fs.readFileSync('apps/luuvcrm/luuvcrm/www/pos.html','utf8');
// Exercise the existing-order payment path, including the guest-check gate.
const start=s.indexOf('  async function _placeOrder(pay){'),end=s.indexOf('    // Editing a sent order:',start);
const fn=s.slice(start,end)+'}';
(async()=>{
 for(const enabled of [false,true]){
  let paid=0,flags=0,messages=[];
  const c={GUEST_CHECK_ENABLED:enabled,cart:{chips:1},document:{getElementById:()=>({value:'Table-01'})},orderType:'Dine In',shiftOpen:true,clearTimeout:()=>{},_saveTimer:null,_saving:false,currentOrder:'test',autosaveTicket:async()=>{},getOrderFlags:async()=>{flags++;return {check_printed:0}},tableOrders:[{name:'test',grand_total:840}],openPayForOrder:()=>paid++,toast:x=>messages.push(x)};
  vm.createContext(c);vm.runInContext(fn,c);await c._placeOrder(true);
  assert.equal(paid,enabled?0:1);assert.equal(flags,enabled?1:0);
  if(enabled) assert.match(messages[0],/guest check first/);
 }
 console.log('PASS: Kurunegala pays without guest check; Dambulla retains guest-check gate');
})().catch(e=>{console.error(e);process.exitCode=1});
