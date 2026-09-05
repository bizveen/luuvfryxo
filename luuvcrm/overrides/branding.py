"""The brand, from settings — not from a literal in three different places.

The app was called `luuvcrm`, `hooks.py` announced "Zeloura Collective", and
every page said "Luuv Fryxo": one product, three names, and the third of them
the first client's. A second restaurant cannot be sold something that calls
itself Luuv Fryxo.

`brand_name` is an owner setting, so it changes in the Desk without a deploy.
Every page context gets it, which is what lets the templates stop hard-coding it.
"""

import frappe

from luuvcrm import settings


def update_website_context(context):
    context["app_name"] = settings.get("brand_name")
    context["brand_name"] = settings.get("brand_name")
    context["brand_short"] = settings.get("brand_short")
    context["brand_html"] = frappe.utils.escape_html(settings.get("brand_name"))


def boot_session(bootinfo):
    name = settings.get("brand_name")
    bootinfo.app_name = name
    if bootinfo.system_settings:
        bootinfo.system_settings.app_name = name
