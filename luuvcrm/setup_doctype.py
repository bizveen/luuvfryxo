import frappe

def execute():
    create_item_review_doctype()
    add_custom_fields()
    frappe.db.commit()
    print("Setup complete")

def create_item_review_doctype():
    if frappe.db.exists("DocType", "Item Review"):
        print("Item Review DocType already exists")
        return
    doc = frappe.get_doc({
        "doctype": "DocType",
        "name": "Item Review",
        "module": "Zeloura",
        "custom": 1,
        "fields": [
            {"fieldname": "item", "label": "Item", "fieldtype": "Link", "options": "Item", "reqd": 1, "in_list_view": 1},
            {"fieldname": "customer_name", "label": "Customer Name", "fieldtype": "Data", "reqd": 1, "in_list_view": 1},
            {"fieldname": "rating", "label": "Rating", "fieldtype": "Rating", "reqd": 1, "in_list_view": 1},
            {"fieldname": "comment", "label": "Comment", "fieldtype": "Small Text"},
            {"fieldname": "submitted_by", "label": "Submitted By", "fieldtype": "Data"},
        ],
        "permissions": [
            {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
            {"role": "Guest", "read": 1, "write": 1, "create": 1},
        ],
    })
    doc.insert()
    print("Created Item Review DocType")

def add_custom_fields():
    fields = [
        {"dt": "Item", "fieldname": "speciality_tags", "label": "Speciality Tags", "fieldtype": "Small Text", "description": "Comma-separated: Chef's Special, Most Popular, Spicy, Vegan, Gluten-Free"},
        {"dt": "Item", "fieldname": "youtube_url", "label": "YouTube Video URL", "fieldtype": "Data"},
        {"dt": "Item", "fieldname": "prep_time", "label": "Preparation Time (mins)", "fieldtype": "Int"},
        {"dt": "Item", "fieldname": "kitchen_group", "label": "Kitchen Group", "fieldtype": "Data"},
        {"dt": "POS Order", "fieldname": "order_type", "label": "Order Type", "fieldtype": "Data", "default": "Dine-In"},
        {"dt": "POS Order Item", "fieldname": "sent_qty", "label": "Sent Qty", "fieldtype": "Int", "default": 0},
        {"dt": "POS Order Item", "fieldname": "notes", "label": "Notes", "fieldtype": "Small Text"},
        {"dt": "Restaurant", "fieldname": "service_charge", "label": "Service Charge (%)", "fieldtype": "Percent", "description": "Service charge percentage added to all orders", "insert_after": "phone"},
        {"dt": "Restaurant", "fieldname": "guide_commission_percent", "label": "Guide Commission (%)", "fieldtype": "Percent", "description": "Guide commission percentage"},
    ]
    for f in fields:
        if not frappe.db.exists("Custom Field", {"dt": f["dt"], "fieldname": f["fieldname"]}):
            cf = frappe.get_doc({
                "doctype": "Custom Field",
                "dt": f["dt"],
                "fieldname": f["fieldname"],
                "label": f["label"],
                "fieldtype": f["fieldtype"],
                "options": f.get("options", ""),
                "description": f.get("description", ""),
                "default": f.get("default", ""),
                "insert_after": f.get("insert_after", ""),
            })
            cf.insert()
            print(f"Added {f['fieldname']} to {f['dt']}")
        else:
            print(f"{f['fieldname']} already exists")
