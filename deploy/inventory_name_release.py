"""Verified item-name editor release; stable Item IDs and original screens."""
import json
import sys
import pos_inventory_release as release

release.RELEASE='/tmp/luuv-inventory-name-20261112'
release.STATE=release.ROOT.parents[1]/'deploy-state/inventory-name-20261112'
release.FILES=['api/manager_inventory.py','templates/includes/manager_inventory.html']
release.LEGACY=['www/manager.html','www/purchases.html','www/manager_v2.html','www/purchase_v2.html',
    'www/pos.html','www/pos2.html','www/pos2.py','api/inventory.py','api/inventory_sections.py','api/purchases.py','api/assets.py']

if __name__=='__main__':
    mode=sys.argv[1]
    if mode=='snapshot':release.snapshot()
    elif mode=='verify':release.verify()
    elif mode in ('stage','deploy'):
        before=json.loads((release.STATE/'release-snapshot.json').read_text())
        for rel in sys.argv[2:] or release.FILES:
            assert rel in release.FILES
            expected=before[rel]['sha256'] if mode=='deploy' else None
            root=release.REMOTE if mode=='deploy' else release.RELEASE+'/stage/luuvcrm'
            release.upload(rel,root+'/'+rel,expected)
    else:raise SystemExit('Use snapshot, stage, deploy or verify')
