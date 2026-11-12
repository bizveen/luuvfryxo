"""Item unit management release with atomic, digest-verified files."""
import json,sys
import pos_inventory_release as release
release.RELEASE='/tmp/luuv-inventory-units-20261112'
release.STATE=release.ROOT.parents[1]/'deploy-state/inventory-units-20261112'
release.FILES=['api/inventory_units.py','api/manager_inventory.py','api/purchases.py',
    'templates/includes/manager_inventory.html','www/purchase_v2.html']
release.LEGACY=['www/manager.html','www/purchases.html','www/waiter.html','www/waiter_v2.html',
    'www/pos.html','www/pos2.html','www/pos2.py','api/inventory.py','api/payments.py']
if __name__=='__main__':
    mode=sys.argv[1]
    if mode=='snapshot':release.snapshot()
    elif mode=='verify':release.verify()
    elif mode in ('stage','deploy'):
        before=json.loads((release.STATE/'release-snapshot.json').read_text())
        for rel in sys.argv[2:] or release.FILES:
            assert rel in release.FILES
            expected=(before[rel]['sha256'] if before[rel] else 'ABSENT') if mode=='deploy' else None
            root=release.REMOTE if mode=='deploy' else release.RELEASE+'/stage/luuvcrm'
            release.upload(rel,root+'/'+rel,expected)
