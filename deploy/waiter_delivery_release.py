"""Waiter 2.0 and guarded delivered-item removal release."""
import json,sys
import pos_inventory_release as release
release.RELEASE='/tmp/luuv-waiter-delivery-20261112'
release.STATE=release.ROOT.parents[1]/'deploy-state/waiter-delivery-20261112'
release.FILES=['api/waiter_delivery.py','api/approvals.py','api/orders.py','hooks.py',
    'www/waiter_v2.py','www/waiter_v2.html','www/pos.html','www/manager_v2.html',
    'templates/includes/delivery_removal.html','templates/includes/removal_report.html',
    'patches/v1_0/waiter_delivery.py','patches.txt','setup_doctype.py']
release.LEGACY=['www/waiter.html','www/waiter.py','www/pos2.html','www/pos2.py',
    'www/manager.html','www/purchases.html','www/purchase_v2.html','api/kitchen.py','api/payments.py']
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
