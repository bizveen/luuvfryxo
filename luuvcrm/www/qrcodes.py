import frappe

no_cache = 1
no_sitemap = 1

def get_context(context):
    context.no_sidebar = True
    context.no_header = True
    context.no_footer = True

    base_url = frappe.utils.get_url()
    tables = frappe.get_all("Restaurant Table", fields=["name"], order_by="name asc")
    qr_list = []
    for t in tables:
        qr_list.append({
            "name": t.name,
            "url": base_url + "/kiosk?table=" + t.name,
        })
    context.qr_list = qr_list
    context.base_url = base_url
