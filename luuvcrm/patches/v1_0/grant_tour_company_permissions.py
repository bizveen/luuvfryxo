"""Give the Tour Company DocType the permissions the Manager app and the till need.

The DocType ships as JSON with System Manager only — the roles are the app's, and
`roles.APP_DOCPERMS` says who else: managers and owners make and change one, the
till reads them. A fresh site gets that from `setup_doctype`; a site that already
exists has had its permissions granted by a patch that ran before this DocType
existed, so nothing would ever grant them here. `ensure_app_docperms` only adds,
so nobody loses anything.
"""

import frappe

from luuvcrm import roles
from luuvcrm import settings


def execute():
    if settings.passive():
        return  # a site that only carries the app; see settings.KEYS "passive"
    granted = roles.ensure_app_docperms()
    if granted:
        frappe.logger().info(f"luuvcrm: granted app docperms {sorted(granted)}")
