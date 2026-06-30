import frappe

def create():
    if frappe.db.exists("DocType", "Zeloura Theme"):
        # Delete existing records
        frappe.db.delete("Zeloura Theme", {"name": ["!=", ""]})
        frappe.db.commit()
        return "Zeloura Theme records cleared"
    
    fields = [
        {"fieldname": "theme_name", "label": "Theme Name", "fieldtype": "Data", "reqd": 1, "in_list_view": 1},
        {"fieldname": "route", "label": "Route", "fieldtype": "Data", "reqd": 1, "in_list_view": 1},
        {"fieldname": "enabled", "label": "Enabled", "fieldtype": "Check", "default": 1, "in_list_view": 1},
        {"fieldname": "sb_css", "label": "CSS Variables", "fieldtype": "Section Break"},
        {"fieldname": "cb1", "label": "", "fieldtype": "Column Break"},
        {"fieldname": "bg_color", "label": "Background", "fieldtype": "Color", "default": "#FAF8F5"},
        {"fieldname": "text_color", "label": "Text", "fieldtype": "Color", "default": "#3D3A3A"},
        {"fieldname": "muted_color", "label": "Muted", "fieldtype": "Color", "default": "#8A8686"},
        {"fieldname": "accent_color", "label": "Accent", "fieldtype": "Color", "default": "#9CAF9A"},
        {"fieldname": "cb2", "label": "", "fieldtype": "Column Break"},
        {"fieldname": "accent_light", "label": "Accent Light", "fieldtype": "Color", "default": "#C5D5C3"},
        {"fieldname": "surface_color", "label": "Surface", "fieldtype": "Color", "default": "#FFFFFF"},
        {"fieldname": "border_color", "label": "Border", "fieldtype": "Color", "default": "#E5E0DA"},
        {"fieldname": "surface_alt", "label": "Surface Alt", "fieldtype": "Color", "default": "#F5F0EB"},
        {"fieldname": "sb_fonts", "label": "Fonts", "fieldtype": "Section Break"},
        {"fieldname": "display_font", "label": "Display Font", "fieldtype": "Data", "default": "Playfair Display"},
        {"fieldname": "body_font", "label": "Body Font", "fieldtype": "Data", "default": "Inter"},
        {"fieldname": "hero_layout", "label": "Hero Layout", "fieldtype": "Int", "default": 1},
    ]
    
    doc = frappe.get_doc({
        "doctype": "DocType",
        "name": "Zeloura Theme",
        "module": "Zeloura",
        "custom": 1,
        "permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
        "fields": fields,
    })
    doc.insert()
    frappe.db.commit()
    return "Zeloura Theme DocType created!"
