"""Give an existing site the Settings fields a fresh one gets.

`settings.KEYS` grew `outlet_line` and `contact_phone` when the printed bill
stopped naming one client. On a fresh site `after_install` builds their fields
on the LuuvCRM Settings single; on a site that already exists nothing did, so
`settings.get()` would have kept handing back the declared default with no way
for the owner to change it — the printed bill would have said Dambulla forever.

Not `after_migrate`: `test_setup.TestTheHookIsWired` says so, and the reason
holds — a live deploy is the wrong place to find out that a "create-only" step
was not. A numbered patch runs once, is recorded, and is the same path every
other schema change here takes. **Adding a key to settings.KEYS after this needs
a patch of its own** — that is the cost of the rule, and it is a line of code.
"""

from luuvcrm.setup_doctype import ensure_settings_single
from luuvcrm import settings


def execute():
    if settings.passive():
        return  # a site that only carries the app; see settings.KEYS "passive"
    ensure_settings_single()
