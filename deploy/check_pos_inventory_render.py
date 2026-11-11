"""Fetch native rendered inline scripts, then syntax-check them locally."""
import base64
import json
from pathlib import Path
import subprocess
import sys
from run_inventory_remote import SSH
from pos_inventory_release import STATE

source = Path(__file__).with_name('pos_inventory_verify.py').read_bytes()
staged = ' --staged' if '--staged' in sys.argv else ''
result = subprocess.run(SSH + ['docker exec -i -w /home/frappe/frappe-bench/sites frappe_docker-backend-1 /home/frappe/frappe-bench/env/bin/python -' + staged],
                        input=source, capture_output=True)
if result.returncode:
    print(result.stderr.decode('utf-8', 'replace'))
    raise SystemExit(result.returncode)
for line in result.stdout.decode().splitlines():
    if line.startswith('SCRIPTS:'):
        scripts = json.loads(base64.b64decode(line.split(':', 1)[1]))
        for route, script in scripts.items():
            path = STATE / (route + ('-staged' if staged else '-live') + '.js')
            path.write_text(script, encoding='utf-8')
            subprocess.run(['node', '--check', str(path)], check=True)
            print('PASS rendered JavaScript syntax /' + route)
    else:
        print(line)
