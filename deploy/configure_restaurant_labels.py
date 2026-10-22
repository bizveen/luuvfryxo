"""Set the approved restaurant brands/cities without changing branch IDs or URLs.

Run from the fryxo site's bench Python context. Existing linked accounting,
POS, staff, and order records keep their original Restaurant identifiers.
"""
import json
from pathlib import Path

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.custom.doctype.property_setter.property_setter import make_property_setter


LABELS = {
    "Luuv Fryxo": ("Rangiri Luuv", "Dambulla"),
    "Kurunegala": ("Luuv Fryxo", "Kurunegala"),
}


def execute():
    assert frappe.local.site == "fryxo", "This data configuration is for fryxo only"
    before = {key: frappe.get_doc("Restaurant", key).as_dict() for key in LABELS}
    backup = Path(frappe.get_site_path("private", "backups", "restaurant-labels-20261022.json"))
    if not backup.exists():
        backup.write_text(json.dumps(before, default=str, indent=2), encoding="utf-8")
    create_custom_fields({"Restaurant": [
        {"fieldname": "restaurant_name", "label": "Restaurant Name", "fieldtype": "Data",
         "insert_after": "image", "in_list_view": 1, "in_global_search": 1},
        {"fieldname": "city", "label": "City", "fieldtype": "Data",
         "insert_after": "restaurant_name", "in_list_view": 1},
    ]})
    for key, (name, city) in LABELS.items():
        doc = frappe.get_doc("Restaurant", key)
        doc.restaurant_name = name
        doc.city = city
        doc.save()
        assert doc.active_menu == before[key]["active_menu"]
        assert doc.company == before[key]["company"]
        assert doc.warehouse == before[key]["warehouse"]
        assert doc.cost_center == before[key]["cost_center"]
    make_property_setter("Restaurant", None, "title_field", "restaurant_name", "Data", for_doctype=True)
    make_property_setter("Restaurant", None, "show_title_field_in_link", "1", "Check", for_doctype=True)
    frappe.db.commit()
    frappe.clear_cache(doctype="Restaurant")
    from frappe.website.utils import clear_website_cache
    clear_website_cache()
    return {key: {"restaurant_name": value[0], "city": value[1]} for key, value in LABELS.items()}
