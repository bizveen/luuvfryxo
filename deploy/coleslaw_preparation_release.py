"""Branch preparation and coleslaw recipe draft release."""
import json,sys
from recipe_sheets_release import release
release.RELEASE='/tmp/luuv-coleslaw-preparation-20261112'
release.STATE=release.ROOT.parents[1]/'deploy-state/coleslaw-preparation-20261112'
release.FILES=['api/manager_inventory.py','api/manager_preparation.py','api/inventory_purchasing.py',
    'recipe_sheets.py','templates/includes/manager_inventory.html','data/coleslaw-sample.json',
    'patches/v1_0/manager_preparation.py','patches.txt']
if __name__=='__main__':
    mode=sys.argv[1]
    if mode=='snapshot':release.snapshot()
    elif mode=='verify':release.verify()
    elif mode in ('stage','deploy'):
        before=json.loads((release.STATE/'release-snapshot.json').read_text())
        for rel in sys.argv[2:] or release.FILES:
            assert rel in release.FILES
            expected=(before[rel]['sha256'] if before[rel] else 'ABSENT') if mode=='deploy' else None
            target=release.REMOTE if mode=='deploy' else release.RELEASE+'/stage/luuvcrm'
            release.upload(rel,target+'/'+rel,expected)
    else:raise SystemExit('Use snapshot, stage, deploy or verify')
