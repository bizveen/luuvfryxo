app_name = "luuvcrm"
app_title = "Zeloura"
app_publisher = "Zeloura Collective"
app_description = "Zeloura Collective — Network Fashion Platform"
app_email = "hello@zeloura.com"
app_license = "mit"

app_include_css = "/assets/luuvcrm/css/luuvcrm_desk.css"

extend_website_context = ["luuvcrm.overrides.branding.update_website_context"]
boot_session = ["luuvcrm.overrides.branding.boot_session"]

# /app = Frappe Desk (bare /app is untouched — login works); only customer
# deep links (?promo= / ?ref= / ?phone=) redirect to the customer PWA at
# /customer. See api.route_app_requests.
before_request = ["luuvcrm.api.route_app_requests"]

# Staff activity trail (luuvcrm/activity.py) — deployed 2026-09-02 with the
# activity stack. Runs in the request's finally block, after the DB has been
# committed/rolled back, so it commits its own row.
# NOTE: activity.py MUST be deployed before this file — the hook imports it on
# every request. (after_migrate → setup_doctype stays repo-only for now: that
# module is not on the server.)
after_request = ["luuvcrm.activity.log_request_activity"]

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