"""Publish the Manager 2.0 active-recipe view."""
import json,sys
from recipe_sheets_release import release
release.RELEASE='/tmp/luuv-recipe-activation-20261113'
release.STATE=release.ROOT.parents[1]/'deploy-state/recipe-activation-20261113'
release.FILES=['templates/includes/manager_inventory.html']
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
