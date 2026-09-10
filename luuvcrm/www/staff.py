"""/staff — where a person lands after signing in, and the installed staff app.

An owner or manager is shown the apps they can open and picks one. Floor staff
are sent straight to their own screen unless they asked for the tiles
(`?choose=1`, which is what the in-app switch links to). See `luuvcrm/launcher.py`
for why it is split that way.

This is also the start URL of the staff PWA (`manifest?app=staff`, scope `/`), so
one icon on a tablet's home screen opens whichever app the person signing in
belongs in — rather than one icon per app, installed per person.
"""

import frappe

from luuvcrm import launcher

no_cache = 1
no_sitemap = 1


def get_context(context):
    context.no_sidebar = True
    context.no_header = True
    context.no_footer = True

    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/pos-login?redirect=/staff"
        raise frappe.Redirect

    apps = launcher.apps_for()
    if not apps:
        # Signed in, but holding none of the restaurant roles — a customer
        # account, or staff whose roles were taken away. Say so rather than
        # forward them to a page that will refuse them.
        frappe.throw(frappe._("This account cannot open any of the staff apps. "
                              "Ask the owner to give it a role."),
                     frappe.PermissionError)

    asked = str(frappe.local.form_dict.get("choose") or "") in ("1", "true")
    if len(apps) == 1 or not (asked or launcher.chooses()):
        frappe.local.flags.redirect_location = apps[0]["path"]
        raise frappe.Redirect

    user = frappe.session.user
    context.apps = apps
    context.csrf_token = frappe.sessions.get_csrf_token()
    context.current_user_full = (frappe.get_value("User", user, "full_name")
                                 or user.split("@")[0].title())
    return context
