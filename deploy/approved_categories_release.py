"""Release approved purchase category corrections without changing legacy pages."""
import json
import sys
import pos_inventory_release as release

release.RELEASE = '/tmp/luuv-approved-categories-20261112'
release.STATE = release.ROOT.parents[1] / 'deploy-state/approved-categories-20261112'
release.FILES = ['api/purchase_categories.py', 'templates/includes/purchase_category_correction.html',
                 'www/manager_v2.html']
release.LEGACY = ['www/manager.html', 'www/purchases.html', 'www/pos.html', 'www/pos2.html',
                  'www/pos2.py', 'api/purchases.py', 'api/approvals.py', 'api/inventory.py']

if __name__ == '__main__':
    mode = sys.argv[1]
    if mode == 'snapshot': release.snapshot()
    elif mode == 'verify': release.verify()
    elif mode in ('stage', 'deploy'):
        before = json.loads((release.STATE / 'release-snapshot.json').read_text())
        for rel in sys.argv[2:] or release.FILES:
            assert rel in release.FILES
            expected = (before[rel]['sha256'] if before[rel] else 'ABSENT') if mode == 'deploy' else None
            root = release.REMOTE if mode == 'deploy' else release.RELEASE + '/stage/luuvcrm'
            release.upload(rel, root + '/' + rel, expected)
    else: raise SystemExit('Use snapshot, stage, deploy or verify')
