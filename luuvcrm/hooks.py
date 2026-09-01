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

# Staff activity trail (luuvcrm/activity.py). Runs in the request's finally block,
# after the DB has already been committed/rolled back, so it commits its own row.
# Layer 1 of three — see the module docstring for the other two.
after_request = ["luuvcrm.activity.log_request_activity"]

scheduler_events = {
    "daily": [
        # Plain rows kept 90 days, flagged rows a year. pos2 writes a lot.
        "luuvcrm.activity.purge_activity_log",
    ],
}

# Idempotent field/DocType provisioning on every migrate (praveen branch).
after_migrate = ["luuvcrm.setup_doctype.execute"]

doc_events = {
    "WhatsApp Message": {
        "after_insert": "luuvcrm.api.process_wa_reply",
    },
}
