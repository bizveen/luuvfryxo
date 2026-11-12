"""Keep recipe review notes aligned with the confirmed packaging breakdown."""
import json,sys
from recipe_sheets_release import release
release.RELEASE='/tmp/luuv-loaded-fries-packaging-20261112'
release.STATE=release.ROOT.parents[1]/'deploy-state/loaded-fries-packaging-20261112'
release.FILES=['recipe_sheets.py']
if __name__=='__main__':
    mode=sys.argv[1]
    if mode=='snapshot':release.snapshot()
    elif mode=='verify':release.verify()
    elif mode in ('stage','deploy'):
        before=json.loads((release.STATE/'release-snapshot.json').read_text())
        for rel in release.FILES:
            expected=before[rel]['sha256'] if mode=='deploy' else None
            root=release.REMOTE if mode=='deploy' else release.RELEASE+'/stage/luuvcrm'
            release.upload(rel,root+'/'+rel,expected)
