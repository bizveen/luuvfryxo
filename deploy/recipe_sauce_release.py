"""Keep recipe draft review notes in sync with confirmed quantities."""
import json,sys
import pos_inventory_release as release
release.RELEASE='/tmp/luuv-recipe-sauce-30g-20261112'
release.STATE=release.ROOT.parents[1]/'deploy-state/recipe-sauce-30g-20261112'
release.FILES=['recipe_sheets.py']
release.LEGACY=['www/manager.html','www/purchases.html','www/waiter.html','www/waiter_v2.html',
    'www/pos.html','www/pos2.html','www/pos2.py','api/inventory.py','api/payments.py']
if __name__=='__main__':
    mode=sys.argv[1]
    if mode=='verify':release.verify()
    elif mode in ('stage','deploy'):
        before=json.loads((release.STATE/'release-snapshot.json').read_text())
        for rel in release.FILES:
            expected=before[rel]['sha256'] if mode=='deploy' else None
            root=release.REMOTE if mode=='deploy' else release.RELEASE+'/stage/luuvcrm'
            release.upload(rel,root+'/'+rel,expected)
