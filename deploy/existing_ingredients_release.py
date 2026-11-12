"""Existing-ingredient registration release, with guarded atomic uploads."""
import json,sys
import pos_inventory_release as release
release.RELEASE='/tmp/luuv-existing-ingredients-20261112'
release.STATE=release.ROOT.parents[1]/'deploy-state/existing-ingredients-20261112'
release.FILES=['api/manager_inventory.py','templates/includes/manager_inventory.html']
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
