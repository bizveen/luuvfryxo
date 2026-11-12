"""Atomic, digest-verified Manager 2.0 equipment inventory release."""
import json
import sys
import pos_inventory_release as release

release.RELEASE = '/tmp/luuv-equipment-inventory-20261112'
release.STATE = release.ROOT.parents[1] / 'deploy-state/equipment-inventory-20261112'
release.FILES = ['api/inventory_sections.py', 'api/manager_inventory.py',
    'templates/includes/manager_inventory.html', 'patches/v1_0/inventory_sections.py',
    'patches.txt', 'setup_doctype.py', 'api/inventory.py']
release.LEGACY = ['www/manager.html', 'www/purchases.html', 'www/manager_v2.html', 'www/purchase_v2.html',
    'www/pos.html', 'www/pos2.html', 'www/pos2.py', 'api/purchases.py', 'api/assets.py']

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
            release.upload(rel, root+'/'+rel, expected)
    else: raise SystemExit('Use snapshot, stage, deploy or verify')
