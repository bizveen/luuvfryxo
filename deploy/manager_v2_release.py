"""Stage/deploy this exact release one atomic, digest-verified file at a time."""
import base64
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys

from run_inventory_remote import SSH

ROOT=Path(__file__).resolve().parents[1]
REMOTE='/home/frappe/frappe-bench/apps/luuvcrm/luuvcrm'
RELEASE='/tmp/luuv-manager-v2-20261111'
STATE=ROOT.parents[1]/'deploy-state/manager-v2-20261111'
FILES=[
    'stock_policy.py','inventory_planning.py','branch_stock_entry.py',
    'api/branch_stock_policy.py','api/manager_inventory.py','api/inventory_purchasing.py',
    'api/inventory.py','patches/v1_0/manager_inventory_versions.py',
    'templates/includes/manager_inventory.html','templates/includes/purchases_stock.html',
    'www/manager_v2.py','www/manager_v2.html','www/purchase_v2.py','www/purchase_v2.html','hooks.py',
]
LEGACY=['www/manager.html','www/purchases.html','www/manager.py','www/purchases.py',
        'www/manager_sw.js','www/purchases_sw.js','www/pos2.html','www/pos2.py']

def remote(code,data=None):
    command='docker exec -i -u root frappe_docker-backend-1 /home/frappe/frappe-bench/env/bin/python -c '+shlex.quote(code)
    result=subprocess.run(SSH+[command],input=data,capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.decode('utf-8','replace'))
    return result.stdout

def snapshot():
    STATE.mkdir(parents=True,exist_ok=True)
    code='''from pathlib import Path
import base64,json,hashlib,shutil
root=Path(%r);release=Path(%r)
assert not release.exists(), 'Release folder already exists; inspect it before restarting.'
release.mkdir()
shutil.copytree(root,release/'stage/luuvcrm',ignore=shutil.ignore_patterns('__pycache__'))
result={}
for rel in %r:
 p=root/rel
 raw=p.read_bytes() if p.exists() else None
 result[rel]=dict(sha256=hashlib.sha256(raw).hexdigest(),data=base64.b64encode(raw).decode()) if raw is not None else None
(release/'snapshot.json').write_text(json.dumps(result))
print(json.dumps(result))
'''%(REMOTE,RELEASE,FILES+LEGACY)
    raw=remote(code)
    (STATE/'snapshot.json').write_bytes(raw)
    snap=json.loads(raw)
    for rel in FILES:
        if snap[rel] is None:continue
        head=subprocess.run(['git','show','HEAD:luuvcrm/'+rel],cwd=ROOT,capture_output=True)
        assert head.returncode==0 and hashlib.sha256(head.stdout).hexdigest()==snap[rel]['sha256'], 'Sync live baseline first: '+rel
    print('Captured live baseline and staged source; all existing release files match Git baseline.',flush=True)

def upload(rel,destination,expected=None):
    raw=(ROOT/'luuvcrm'/rel).read_bytes()
    digest=hashlib.md5(raw).hexdigest()
    code='''from pathlib import Path
import sys,base64,hashlib,os
path=Path(%r);expected=%r
if expected is not None:
 actual=hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else 'ABSENT'
 assert actual==expected, 'Server file changed since snapshot: '+str(path)
data=base64.b64decode(sys.stdin.buffer.read(),validate=True)
assert hashlib.md5(data).hexdigest()==%r, 'Transfer digest mismatch'
path.parent.mkdir(parents=True,exist_ok=True)
tmp=path.with_name(path.name+'.release-tmp')
tmp.write_bytes(data);os.chown(tmp,1001,1001);os.chmod(tmp,0o664)
os.replace(tmp,path)
assert hashlib.md5(path.read_bytes()).hexdigest()==%r
print('Verified '+str(path))
'''%(destination,expected,digest,digest)
    remote(code,base64.b64encode(raw))
    print('Verified '+rel+' '+digest,flush=True)

def verify():
    code='''from pathlib import Path
import hashlib,json
root=Path(%r)
print(json.dumps({rel:hashlib.sha256((root/rel).read_bytes()).hexdigest() for rel in %r}))
'''%(REMOTE,FILES+LEGACY)
    result=json.loads(remote(code))
    before=json.loads((STATE/'snapshot.json').read_text())
    for rel in LEGACY:
        assert result[rel]==before[rel]['sha256'], 'Original file changed: '+rel
    for rel in FILES:
        assert result[rel]==hashlib.sha256((ROOT/'luuvcrm'/rel).read_bytes()).hexdigest(),rel
    (STATE/'deployed-sha256.json').write_text(json.dumps(result,indent=2))
    print('All 15 release files match PC; all 8 original page/controller/PWA/POS files unchanged.')

if __name__=='__main__':
    mode=sys.argv[1]
    if mode=='snapshot':snapshot()
    elif mode in ('stage','deploy'):
        before=json.loads((STATE/'snapshot.json').read_text())
        selected=sys.argv[2:] or FILES
        assert set(selected)<=set(FILES), 'Only release manifest files can be uploaded'
        for rel in selected:
            expected=(before[rel]['sha256'] if before[rel] else 'ABSENT') if mode=='deploy' else None
            target=(REMOTE if mode=='deploy' else RELEASE+'/stage/luuvcrm')+'/'+rel
            upload(rel,target,expected)
    elif mode=='verify':verify()
    else:raise SystemExit('Use snapshot, stage, deploy or verify')
