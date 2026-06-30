import frappe

def update_website_context(context):
    context["app_name"] = "Zeloura"
    context["brand_name"] = "Zeloura"
    context["brand_html"] = "Zeloura"

def boot_session(bootinfo):
    bootinfo.app_name = "Zeloura"
    if bootinfo.system_settings: bootinfo.system_settings.app_name = "Zeloura"
