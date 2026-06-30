import frappe, json

pages = frappe.db.get_all("Web Page", fields=["route", "title", "published"], order_by="route asc")
website_menu = frappe.db.get_all("Top Bar Item", fields=["label", "url", "parent_label"], order_by="idx")

print("=== Web Pages ===")
for p in pages:
    print(f"  /{p['route']:25s} | {'PUBLISHED' if p['published'] else 'DRAFT':10s} | {p['title'] or ''}")

print("\n=== Top Bar Menu ===")
for m in website_menu:
    p = f"  [{m['label']}] -> {m['url']}" 
    if m['parent_label']:
        p += f"  (child of {m['parent_label']})"
    print(p)

# Also check www/ files
import os
www_dir = "/home/frappe/frappe-bench/sites/clothing/public/www"
if os.path.isdir(www_dir):
    print("\n=== Custom www/ files ===")
    for f in sorted(os.listdir(www_dir)):
        print(f"  /{f}")
