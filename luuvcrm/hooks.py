app_name = "luuvcrm"
app_title = "Zeloura"
app_publisher = "Zeloura Collective"
app_description = "Zeloura Collective — Network Fashion Platform"
app_email = "hello@zeloura.com"
app_license = "mit"

app_include_css = "/assets/luuvcrm/css/luuvcrm_desk.css"

extend_website_context = ["luuvcrm.overrides.branding.update_website_context"]
boot_session = ["luuvcrm.overrides.branding.boot_session"]

doc_events = {
    "WhatsApp Message": {
        "after_insert": "luuvcrm.api.process_wa_reply",
    },
}