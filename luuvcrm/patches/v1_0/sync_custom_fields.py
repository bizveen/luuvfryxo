"""Apply the declared custom fields to a site that already exists.

`CUSTOM_FIELDS` in setup_doctype.py declares the fields the code reads and
writes but no DocType JSON defines, and `after_install` applies them — which
covers a new site and nothing else. On a site that is already running, the only
thing that had ever created them was somebody calling the matching `setup_*`
endpoint over HTTP. So the declaration was the source of truth for a fresh
install and a wish list for the live one, and a field added to it would never
have arrived: `bench migrate` had no step that looked.

`add_custom_fields()` skips a field that is already there and a DocType this
site does not have, so on the live restaurant this is expected to add nothing —
it is the guarantee that matters, not the diff.

A patch runs once, which is deliberate (`after_migrate` is ruled out in
test_setup.TestTheHookIsWired: every step is create-only, but a live deploy is
the wrong place to find out we were wrong about that). The cost is the same as
for settings keys: the *next* field added to CUSTOM_FIELDS needs a patch of its
own. One line, and it is written down.
"""

import frappe

from luuvcrm.setup_doctype import add_custom_fields
from luuvcrm import settings


def execute():
    if settings.passive():
        return  # a site that only carries the app; see settings.KEYS "passive"
    add_custom_fields()
    frappe.clear_cache()
