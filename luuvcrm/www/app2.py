"""/app2 — Frappe's default desk.

Frappe serves its desk from ``frappe/www/app.html`` at ``/app``, and maps
``/app/<route>`` onto the same endpoint (``website_route_rules`` in
``frappe/hooks.py``). ``luuvcrm/www/app.py`` (the customer PWA) sits on that same
route and, because luuvcrm is resolved before frappe, it shadows the desk
completely. This module re-hosts the untouched desk at ``/app2`` instead.

Three pieces make it work:
  * ``get_context`` here just delegates to frappe's own desk context builder —
    nothing is copied, so a frappe upgrade can't leave this behind.
  * ``website_route_rules`` in ``luuvcrm/hooks.py`` maps ``/app2/<route>`` here,
    the same way frappe maps ``/app/<route>``.
  * ``app2.html`` includes frappe's desk markup and then patches the three spots
    in ``frappe/public/js/frappe/router.js`` that hardcode the ``/app`` prefix,
    so desk navigation stays inside ``/app2``.
"""

import frappe
from frappe.www import app as desk

no_cache = 1


def get_context(context):
    # app2.html is a one-line {% include %} of frappe's desk template. Without
    # this, TemplatePage.set_properties_from_source() sees a source with no
    # "</body>" and no "{% extends %}" and wraps the whole desk inside the
    # website base template.
    context.base_template = ""

    return desk.get_context(context)


def redirect_desk_login(login_manager=None):
    """``on_session_creation`` hook.

    frappe points System Users at ``/app`` after login (``set_user_info`` in
    ``frappe/auth.py``), which here is the customer PWA. Send them to the desk.
    Website Users are left alone — ``/app`` is exactly where they belong.
    """
    info = getattr(login_manager, "info", None)
    if not info or info.get("user_type") != "System User":
        return

    home = frappe.local.response.get("home_page") or ""
    if home == "/app" or home.startswith("/app/"):
        frappe.local.response["home_page"] = "/app2" + home[len("/app") :]
