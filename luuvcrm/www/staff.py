"""/staff — where a person lands after signing in, and the installed staff app.

Floor staff are sent straight to their own screen unless they asked for the tiles
(`?choose=1`, which is what the in-app switch links to). See `luuvcrm/launcher.py`
for why it is split that way.

An owner or manager gets this page, and what it shows depends on two things:

* **Which restaurant, first** — for somebody who can choose one (`branch.can_choose`:
  more than one restaurant, and no User Permission pinning them to one of them).
  Asked on every fresh open, with last time's choice marked, because the choice is
  stored and a step that only appears once would never appear again. Not asked
  when they arrive from the in-app switch: they are moving between apps, not
  starting the day.
* **Tiles on a phone or in the installed app; the back office at a desk.** Only
  the browser knows which it is — `display-mode: standalone` and the viewport
  width — so the page decides, and the server hands it `desktop_home`. The
  switch's `?choose=1` still shows tiles at a desk, because that is somebody
  asking for them.

This is also the start URL of the staff PWA (`manifest?app=staff`, scope `/`), so
one icon on a tablet's home screen opens whichever app the person signing in
belongs in — rather than one icon per app, installed per person.
"""

import frappe

from luuvcrm import branch, launcher

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
        frappe.local.flags.redirect_location = launcher.primary(apps)["path"]
        raise frappe.Redirect

    user = frappe.session.user
    keys = [a["key"] for a in apps]
    context.apps = apps
    context.asked = asked
    context.desktop_home = "/admin" if "admin" in keys else apps[0]["path"]
    context.branches = branch.choices() if branch.can_choose(user) else []
    # Keep the Restaurant document name as the value submitted to the API, but
    # show the same customer-facing name and location used in every operational
    # app header and in the in-app branch switcher.
    for choice in context.branches:
        choice["label"] = branch.display_label(choice["name"])
    context.chosen = branch.selected(user) or ""
    context.chosen_label = branch.display_label(context.chosen) if context.chosen else ""
    context.csrf_token = frappe.sessions.get_csrf_token()
    context.current_user_full = (frappe.get_value("User", user, "full_name")
                                 or user.split("@")[0].title())
    return context
