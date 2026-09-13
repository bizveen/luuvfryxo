"""Which staff apps a person can open, and where they should land after login.

The login screen used to send everybody to `/pos`. That was right for a cashier
and wrong for everyone else: a manager wanted `/admin` or `/manager` and had to
retype a URL, and a waiter or the kitchen — who cannot open the till — were sent
to a 403. Worse, `/waiter` serves the login form in place with no `?redirect`, so
a waiter signing in *from their own app* was sent to the till and refused.

`/staff` is the answer, and the one place this list lives:

* an **owner or manager** gets a chooser — Admin, Manager, Purchases, and the
  floor screens — because they genuinely move between them. On a phone or in the
  installed app, that is; at a desktop browser they go straight to `/admin`, and
  an owner with more than one restaurant picks the restaurant first. Both are
  decided on the page, see `www/staff.py`;
* **floor staff** go straight to their own screen, because a cashier who is shown
  five tiles every morning will tap the wrong one mid-rush.

Every app keeps its own gate. This list only decides what is *offered*; a tile
for a screen the person cannot open would be a 403 with a nicer label, so the
roles here are the same `roles.*_ROLES` each page already checks.

Lives at the top level next to `branch.py`, because the Jinja global that draws
the in-app switch imports it and must not pull in the api package.
"""

import frappe

from luuvcrm import roles

#: In the order they are offered. The floor screens run most-senior-first, so a
#: cashier (who can also open the waiter and kitchen screens) lands on the till —
#: see `primary()` for where somebody who is not shown the tiles goes.
APPS = (
    {"key": "admin", "path": "/admin", "label": "Admin", "icon": "🛠️",
     "about": "Menu, tables, staff and branches", "allowed": roles.MANAGER_ROLES},
    {"key": "manager", "path": "/manager", "label": "Manager", "icon": "📊",
     "about": "Today's sales, approvals and the cash drawer", "allowed": roles.MANAGER_ROLES},
    {"key": "purchases", "path": "/purchases", "label": "Purchases", "icon": "🧾",
     "about": "Supplier bills and what is still owed", "allowed": roles.PURCHASE_ROLES},
    {"key": "pos", "path": "/pos", "label": "POS", "icon": "🍔",
     "about": "Orders and payments at the counter", "allowed": roles.POS_ROLES},
    {"key": "waiter", "path": "/waiter", "label": "Waiter", "icon": "🤵",
     "about": "Take and amend orders at the table", "allowed": roles.WAITER_ROLES},
    {"key": "kitchen", "path": "/kitchen", "label": "Kitchen", "icon": "👨‍🍳",
     "about": "The ticket queue and the pass", "allowed": roles.KITCHEN_ROLES},
)

#: Where the in-app switch sends somebody. `choose=1` shows the tiles even to a
#: person who would otherwise be forwarded straight to their own screen.
CHOOSER = "/staff?choose=1"


def apps_for(user=None):
    """The apps this person can open, in the order they are offered."""
    user = user or frappe.session.user
    if not user or user == "Guest":
        return []
    return [{k: v for k, v in app.items() if k != "allowed"}
            for app in APPS if roles.has_any(app["allowed"], user)]


#: The screens a shift is worked on. Somebody who is not shown the tiles lands
#: on the first of these they can open, before any back-office app.
FLOOR = ("pos", "waiter", "kitchen")


def primary(apps):
    """Where somebody who is not shown the tiles is sent.

    Their floor screen if they have one, otherwise the first app they can open. A
    cashier who also buys for the kitchen lands on the till, because that is what
    the morning needs, and reaches Purchases through the switch; a Purchaser and
    nothing else goes straight to Purchases.
    """
    for app in apps:
        if app["key"] in FLOOR:
            return app
    return apps[0]


def chooses(user=None):
    """Whether this person is shown the tiles by default rather than forwarded.

    Managers and above: they move between the back office and the floor all day.
    Everyone else lands on their own screen and can still reach the tiles through
    the switch, if they can open more than one.
    """
    return roles.is_manager(user or frappe.session.user)


#: Where signing out leaves somebody: the login form, which sends the next person
#: to `/staff` like any other sign-in.
SIGNED_OUT = "/pos-login"


@frappe.whitelist(allow_guest=True)
def sign_out():
    """Sign out and land on the login form.

    Every staff app linked to Frappe's `/api/method/logout`. That ends the session
    and then answers with JSON — no redirect, and it ignores `redirect-to` — so the
    screen turned into `{"message":"No App",...}`. In a browser tab that looks
    broken; in the installed app, which has no address bar, it is a dead end, and
    it was reported as "the logout button does not work". GET, because it is a
    link; allow_guest, because tapping it twice must not be a 403.
    """
    if frappe.session.user != "Guest":
        frappe.local.login_manager.logout()
        frappe.db.commit()
    frappe.local.response["type"] = "redirect"
    frappe.local.response["location"] = SIGNED_OUT


def staff_apps(context=None):
    """Jinja global for `templates/includes/app_switch.html`. Guest gets nothing,
    so the switch never draws on a login form rendered in place."""
    try:
        return apps_for()
    except Exception:
        return []
