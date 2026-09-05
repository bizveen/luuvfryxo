"""The brand, from settings — not from a literal in three different places.

The app was called `luuvcrm`, `hooks.py` announced "Zeloura Collective", and
every page said "Luuv Fryxo" in an accent colour written out as `#F15207`
twelve times over. One product, three names and a hard-coded palette: a second
restaurant cannot be sold something that calls itself Luuv Fryxo and is orange
whatever it does.

`update_website_context` runs for every page Frappe renders, which makes it the
one place to put this. Each page includes `templates/includes/brand.html`, which
turns these values into CSS custom properties — so the accent on every screen is
one field in the Desk.
"""

import frappe

from luuvcrm import settings


def brand(context=None):
    """The values every page needs to look like this restaurant."""
    return {
        "brand_name": settings.get("brand_name"),
        "brand_short": settings.get("brand_short"),
        "brand_color": settings.get("brand_color"),
        "brand_color_dark": settings.get("brand_color_dark"),
        "brand_logo": settings.get("brand_logo"),
        "brand_icon_192": settings.get("brand_icon_192"),
        "brand_icon_512": settings.get("brand_icon_512"),
    }


def update_website_context(context):
    values = brand()
    context.update(values)
    context["app_name"] = values["brand_name"]
    context["brand_html"] = frappe.utils.escape_html(values["brand_name"])


def boot_session(bootinfo):
    name = settings.get("brand_name")
    bootinfo.app_name = name
    if bootinfo.system_settings:
        bootinfo.system_settings.app_name = name
