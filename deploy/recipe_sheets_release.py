"""Publish the supplied recipe sheets inside Manager 2.0."""
import base64,hashlib,json,subprocess,sys
import pos_inventory_release as release
release.RELEASE='/tmp/luuv-recipe-sheets-20261112'
release.STATE=release.ROOT.parents[1]/'deploy-state/recipe-sheets-20261112'
release.FILES=['api/manager_inventory.py','templates/includes/manager_inventory.html',
    'recipe_sheets.py','api/recipe_sheets.py','data/kurunegala-recipes.json',
    'patches/v1_0/recipe_sheet_drafts.py','patches.txt']
release.LEGACY=['www/manager.html','www/purchases.html','www/waiter.html','www/waiter_v2.html',
    'www/pos.html','www/pos2.html','www/pos2.py','api/inventory.py','api/payments.py']
if __name__=='__main__':
    mode=sys.argv[1]
    if mode=='snapshot':release.snapshot()
    elif mode=='extend-baseline':
        data=release.remote('from pathlib import Path;import base64;print(base64.b64encode(Path(%r).read_bytes()).decode())'%(release.REMOTE+'/patches.txt'))
        raw=base64.b64decode(data)
        head=subprocess.check_output(['git','show','HEAD:luuvcrm/patches.txt'],cwd=release.ROOT)
        if raw!=head:
            import difflib
            (release.STATE/'patches-live.txt').write_bytes(raw)
            print(''.join(difflib.unified_diff(head.decode().splitlines(True),raw.decode().splitlines(True),fromfile='Git',tofile='Live')))
            raise SystemExit('Sync the captured migration list before extending the baseline.')
        path=release.STATE/'release-snapshot.json';before=json.loads(path.read_text())
        assert 'patches.txt' not in before
        before['patches.txt']=dict(sha256=hashlib.sha256(raw).hexdigest(),data=base64.b64encode(raw).decode())
        path.write_text(json.dumps(before));print('Migration list matches live baseline')
    elif mode=='verify':release.verify()
    elif mode in ('stage','deploy'):
        before=json.loads((release.STATE/'release-snapshot.json').read_text())
        for rel in sys.argv[2:] or release.FILES:
            assert rel in release.FILES
            expected=(before[rel]['sha256'] if before[rel] else 'ABSENT') if mode=='deploy' else None
            root=release.REMOTE if mode=='deploy' else release.RELEASE+'/stage/luuvcrm'
            release.upload(rel,root+'/'+rel,expected)
