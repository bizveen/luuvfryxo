"""Deploy only the Manager 2.0 approval UI; existing approval APIs are reused."""
import sys
import pos_inventory_release as release

release.RELEASE = '/tmp/luuv-manager-categories-20261111'
release.STATE = release.ROOT.parents[1] / 'deploy-state/manager-categories-20261111'
release.FILES = ['www/manager_v2.html']
release.LEGACY = ['www/manager.html', 'www/purchases.html', 'www/pos.html',
                  'www/pos2.html', 'www/pos2.py', 'api/purchases.py', 'api/approvals.py']

if __name__ == '__main__':
    mode = sys.argv[1]
    if mode == 'snapshot':
        release.snapshot()
    elif mode == 'verify':
        release.verify()
    elif mode in ('stage', 'deploy'):
        import json
        before = json.loads((release.STATE / 'release-snapshot.json').read_text())
        for rel in release.FILES:
            expected = before[rel]['sha256'] if mode == 'deploy' else None
            target = release.REMOTE if mode == 'deploy' else release.RELEASE + '/stage/luuvcrm'
            release.upload(rel, target + '/' + rel, expected)
    else:
        raise SystemExit('Use snapshot, stage, deploy or verify')
