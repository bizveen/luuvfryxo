"""/owner — folded into /admin, kept as a redirect.

Every branch comparison, the branch setup and the menu editor live in the
portal at /admin now: one page, one login, a section down the side instead of a
second screen to remember. This route stays because it is bookmarked — the same
reason /pos2 stayed for a release after the POS pages collapsed.

The guest case is deliberately not handled here: /admin does its own gate, so a
bookmark opened on a signed-out phone ends at /pos-login and comes back to
/admin, not to the old address.
"""

import frappe

no_cache = 1
no_sitemap = 1


def get_context(context):
    frappe.local.flags.redirect_location = "/admin#branches"
    raise frappe.Redirect
