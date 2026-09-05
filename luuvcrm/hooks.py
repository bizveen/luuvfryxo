app_name = "luuvcrm"
# One product, one name. These are the *app's* identity and are fixed at build
# time, so they stay generic; the restaurant's own name is `brand_name` in
# LuuvCRM Settings and reaches every page through overrides/branding.py.
# They used to read "Zeloura Collective — Network Fashion Platform", which is a
# different product entirely (see the fashion-site code still in the tree).
app_title = "LuuvCRM"
app_publisher = "LuuvCRM"
app_description = "Restaurant POS, kitchen, purchasing and loyalty on ERPNext"
app_email = "admin@luuvcrm.com"
app_license = "mit"

# B4. Not a pyproject dependency — those are pip packages, and these are Frappe
# apps that bench resolves. `frappe.installer.install_app()` reads this hook and
# installs each one first, so `bench install-app luuvcrm` on a clean bench brings
# erpnext and hospitality with it instead of failing at the first import.
#
# hospitality supplies Restaurant, Restaurant Table and Restaurant Menu, and
# declares erpnext as its own requirement — naming both anyway, because the
# order matters and a reader should not have to follow a chain to learn it.
required_apps = ["erpnext", "hospitality"]

app_include_css = "/assets/luuvcrm/css/luuvcrm_desk.css"

# The brand — name, accent, icons — as a Jinja global, so every page can read it
# from templates/includes/brand.html without each one having to put it in its own
# context. A page with no .py at all still gets it.
#
# NOT `extend_website_context`: that hook does not exist in this Frappe, so the
# branding module it pointed at had never once been called.
jinja = {"methods": ["luuvcrm.overrides.branding.brand"]}
boot_session = ["luuvcrm.overrides.branding.boot_session"]

# /app = Frappe Desk (bare /app is untouched — login works); only customer
# deep links (?promo= / ?ref= / ?phone=) redirect to the customer PWA at
# /customer. See api.route_app_requests.
before_request = ["luuvcrm.api.route_app_requests"]

# Staff activity trail (luuvcrm/activity.py) — deployed 2026-09-02 with the
# activity stack. Runs in the request's finally block, after the DB has been
# committed/rolled back, so it commits its own row.
# NOTE: activity.py MUST be deployed before this file — the hook imports it on
# every request.
after_request = ["luuvcrm.activity.log_request_activity"]

# The whole schema — DocTypes, custom fields, property setters, roles, DocPerms,
# the branch column — in luuvcrm/setup_doctype.py. Wired to after_install only,
# so `bench install-app luuvcrm` provisions a fresh site on its own and a deploy
# never touches an existing one. On an existing site, run it by hand:
#   bench --site <site> execute luuvcrm.setup_doctype.report_drift   # read-only
#   bench --site <site> execute luuvcrm.setup_doctype.execute
# Deliberately NOT after_migrate: every step there is create-only, but a live
# deploy is the wrong place to discover we were wrong about that.
#
# It has to carry the patches' work too — Frappe marks every patch as completed
# on a fresh install without running it, so after_install is all a new site gets.
after_install = "luuvcrm.setup_doctype.after_install"

scheduler_events = {
    "daily": [
        # Plain rows kept 90 days, flagged rows a year. The POS writes a lot.
        "luuvcrm.activity.purge_activity_log",
        # Resolved client errors go after a month, open ones after six.
        "luuvcrm.api.errors.purge_client_errors",
    ],
}

doc_events = {
    "WhatsApp Message": {
        "after_insert": "luuvcrm.api.process_wa_reply",
    },
}

# Stamp the branch on every row this app owns, on the way in. A field somebody
# has to remember to set is a field that ends up empty, and an empty branch is a
# row that stays visible to every branch. See luuvcrm/branch.py.
for _branched in (
    "POS Order", "Purchase Bill", "Expense", "Wastage",
    "POS Approval", "POS Cash Movement", "LF Activity Log",
):
    doc_events.setdefault(_branched, {})["before_insert"] = "luuvcrm.branch.stamp"
del _branched