import frappe

no_cache = 1

def get_context(context):
    context.no_sidebar = True
    context.no_header = True
    context.no_footer = True
    redirect = frappe.local.form_dict.get("redirect", "/pos")
    context.redirect = redirect
    context.title = "Login - Luuv Fryxo"
