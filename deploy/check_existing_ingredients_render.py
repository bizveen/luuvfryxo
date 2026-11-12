"""Native page rendering and inline JavaScript syntax verification."""
import base64,re,subprocess,sys
from run_inventory_remote import SSH
from existing_ingredients_release import release
sys.stdout.reconfigure(encoding='utf-8')
staged='--staged' in sys.argv
root=release.RELEASE+'/stage' if staged else release.REMOTE.rsplit('/',1)[0]
source='''import base64,sys
sys.path.insert(0,%r)
from pathlib import Path
from unittest.mock import patch
from werkzeug.wrappers import Request
import frappe,frappe.sessions
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
try:
 from luuvcrm import branch
 from luuvcrm.www import manager_v2,purchase_v2
 from luuvcrm.overrides.branding import update_website_context
 with patch.object(branch,'selected',return_value='Kurunegala'),patch.object(frappe.sessions,'get_csrf_token',return_value='release-render-check'):
  for page,route,controller in [('manager_v2','/manager2.0',manager_v2),('purchase_v2','/purchase2.0',purchase_v2)]:
   frappe.local.request=Request.from_values(base_url='https://luuvgrand.com',path=route);frappe.local.form_dict=frappe._dict()
   context=frappe._dict();controller.get_context(context);update_website_context(context)
   html=frappe.render_template(Path(%r+'/luuvcrm/www/'+page+'.html').read_text(),context)
   assert 'Stock unit' in html and 'Unit locked' in html and '{#' not in html
   print('HTML:'+page+':'+base64.b64encode(html.encode()).decode())
   print('PASS native existing ingredient render '+route)
finally:frappe.db.rollback();frappe.destroy()
'''%(root,root)
result=subprocess.run(SSH+['docker exec -i -w /home/frappe/frappe-bench/sites frappe_docker-backend-1 /home/frappe/frappe-bench/env/bin/python'],input=source.encode(),capture_output=True)
if result.returncode:
    print(result.stderr.decode('utf-8','replace'));raise SystemExit(result.returncode)
for line in result.stdout.decode().splitlines():
    if line.startswith('HTML:'):
        _,page,data=line.split(':',2);html=base64.b64decode(data).decode()
        target=release.STATE/(page+('-staged' if staged else '-live')+'.html');target.write_text(html,encoding='utf-8')
        js=target.with_suffix('.js');js.write_text('\n'.join(re.findall(r'<script\b[^>]*>(.*?)</script>',html,re.S|re.I)),encoding='utf-8')
        subprocess.run(['node','--check',str(js)],check=True)
        print('PASS JavaScript syntax '+page)
    else:print(line)
