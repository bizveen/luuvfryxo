"""Publish lemonade recipe drafts in Manager 2.0."""
import json,sys
from recipe_sheets_release import release
release.RELEASE='/tmp/luuv-lemonade-recipes-20261112'
release.STATE=release.ROOT.parents[1]/'deploy-state/lemonade-recipes-20261112'
release.FILES=['recipe_sheets.py','data/lemonade-recipes.json','templates/includes/manager_inventory.html']
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
