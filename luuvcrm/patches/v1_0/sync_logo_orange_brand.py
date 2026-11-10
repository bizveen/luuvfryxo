"""Align the stored brand accent with the orange used by the logo asset."""

import frappe

from luuvcrm import settings


def execute():
    if settings.passive() or not frappe.db.exists(
        "DocType", settings.SETTINGS_DOCTYPE
    ):
        return

    replacements = {
        "brand_color": ("#F15207", "#FB6C03"),
        "brand_color_dark": ("#D84804", "#E85C00"),
    }
    changed = False
    for fieldname, (old_value, new_value) in replacements.items():
        current = frappe.db.get_single_value(settings.SETTINGS_DOCTYPE, fieldname)
        if current in (None, "", old_value):
            frappe.db.set_single_value(
                settings.SETTINGS_DOCTYPE, fieldname, new_value
            )
            changed = True

    if changed:
        settings.forget()
        frappe.clear_cache()
