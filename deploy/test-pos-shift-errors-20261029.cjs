const fs=require('fs'),vm=require('vm'),assert=require('assert');
const s=fs.readFileSync('apps/luuvcrm/luuvcrm/www/pos.html','utf8');
const part=(a,b)=>s.slice(s.indexOf(a),s.indexOf(b,s.indexOf(a)));
const elements={openProfile:{value:'Kurunegala'},openBtn:{},openErr:{style:{}}};
const c={CSRF:'test',location:{},document:{getElementById:id=>elements[id],querySelectorAll:()=>[]},URLSearchParams};
vm.createContext(c);
vm.runInContext(part('  function _smsg(', '  function fmt(')+part('  async function _api(', '  // ── nav:')+part('  async function openShift(){','  async function openCloseModal('),c);
(async()=>{
 c.fetch=async()=>({status:403,ok:false,json:async()=>({_server_messages:JSON.stringify([JSON.stringify({message:'Missing shift permission'})])})});
 await c.openShift();assert.equal(elements.openErr.textContent,'Missing shift permission');assert.equal(c.location.href,undefined);assert.equal(elements.openBtn.disabled,false);
 c.fetch=async()=>{throw Error('offline')};await c.openShift();assert.match(elements.openErr.textContent,/Could not connect/);assert.equal(elements.openBtn.disabled,false);
 c.fetch=async()=>({status:401});await c._api('/test');assert.equal(c.location.href,'/login?redirect-to=/pos');
 console.log('PASS: 403 inline error, offline inline error, button restored, 401 login redirect');
})().catch(e=>{console.error(e);process.exitCode=1});
