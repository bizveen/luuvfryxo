"""Verified branch category defaults and approval history release."""
import json
import sys
import pos_inventory_release as release
release.RELEASE='/tmp/luuv-manager-history-20261112'
release.STATE=release.ROOT.parents[1]/'deploy-state/manager-history-20261112'
release.FILES=['api/purchase_item_categories.py','api/removal_report.py',
    'api/purchases.py','api/purchase_categories.py','api/approvals.py',
    'www/manager_v2.html','www/purchase_v2.html','templates/includes/removal_report.html',
    'patches/v1_0/purchase_item_defaults.py','patches.txt','setup_doctype.py']
release.LEGACY=['www/manager.html','www/purchases.html','www/pos.html','www/pos2.html','www/pos2.py',
    'api/orders.py','api/inventory.py']
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
    else:raise SystemExit('Use snapshot, stage, deploy or verify')
