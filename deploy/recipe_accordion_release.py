"""Publish the mobile recipe accordion, preserving the original app routes."""
import json
import sys
from recipe_sheets_release import release

release.RELEASE = '/tmp/luuv-recipe-accordion-20261112'
release.STATE = release.ROOT.parents[1] / 'deploy-state/recipe-accordion-20261112'
release.FILES = ['templates/includes/manager_inventory.html']

if __name__ == '__main__':
    mode = sys.argv[1]
    if mode == 'snapshot':
        release.snapshot()
    elif mode == 'verify':
        release.verify()
    elif mode in ('stage', 'deploy'):
        before = json.loads((release.STATE / 'release-snapshot.json').read_text())
        for rel in release.FILES:
            expected = before[rel]['sha256'] if mode == 'deploy' else None
            target = release.REMOTE if mode == 'deploy' else release.RELEASE + '/stage/luuvcrm'
            release.upload(rel, target + '/' + rel, expected)
    else:
        raise SystemExit('Use snapshot, stage, deploy or verify')
