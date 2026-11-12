"""Render the native Purchase 2.0 page and syntax-check its inline JavaScript."""
import base64
import re
import subprocess
import sys
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path
from run_inventory_remote import SSH
from manager_history_release import release

staged = '--staged' in sys.argv
page = (release.RELEASE + '/stage/luuvcrm' if staged else release.REMOTE) + '/www/purchase_v2.html'
source = '''import base64,re,sys
sys.path.insert(0,%r)
from pathlib import Path
from unittest.mock import patch
from werkzeug.wrappers import Request
import frappe
import frappe.sessions
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
try:
 from luuvcrm import branch
 from luuvcrm.www import purchase_v2
 from luuvcrm.overrides.branding import update_website_context
 frappe.local.request=Request.from_values(base_url='https://luuvgrand.com',path='/purchase2.0')
 frappe.local.form_dict=frappe._dict()
 with patch.object(branch,'selected',return_value='Kurunegala'),patch.object(frappe.sessions,'get_csrf_token',return_value='release-render-check'):
  context=frappe._dict();purchase_v2.get_context(context);update_website_context(context)
  html=frappe.render_template(Path(%r).read_text(),context)
  assert 'purchase_item_categories.catalog' in html and 'use_item_categories:1' in html
  assert '{#' not in html
  scripts='\\n'.join(re.findall(r'<script\\b[^>]*>(.*?)</script>',html,re.S|re.I))
  print('SCRIPTS:'+base64.b64encode(scripts.encode()).decode())
  print('PASS native Purchase 2.0 render with saved categories')
finally:frappe.db.rollback();frappe.destroy()
''' % ((release.RELEASE + '/stage') if staged else release.REMOTE.rsplit('/', 1)[0],page)
result = subprocess.run(SSH + ['docker exec -i -w /home/frappe/frappe-bench/sites frappe_docker-backend-1 /home/frappe/frappe-bench/env/bin/python'],
                        input=source.encode(), capture_output=True)
if result.returncode:
    print(result.stderr.decode('utf-8', 'replace'))
    raise SystemExit(result.returncode)
for line in result.stdout.decode().splitlines():
    if line.startswith('SCRIPTS:'):
        dest = release.STATE / ('purchase-defaults-' + ('staged' if staged else 'live') + '.js')
        dest.write_bytes(base64.b64decode(line.split(':', 1)[1]))
        assert dest.stat().st_size > 10000
        subprocess.run(['node', '--check', str(dest)], check=True)
        print('PASS native rendered JavaScript syntax')
    else:
        print(line)
