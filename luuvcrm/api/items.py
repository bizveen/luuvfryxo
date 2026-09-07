"""The item master: everything the restaurant sells or buys, on a menu or not.

The menu is a *selection*. This is the list it selects from — the burger that
is off the menu this season, the bottle of oil that only ever appears on a
purchase bill, the extra cheese that exists to be an add-on. `luuvcrm.api.menu`
owns what is on the menu and what it costs there; this module owns the item
itself, so an item can be created, priced, renamed and retired without a menu
being involved at all.

Deleting is deliberately not here. Every invoice line, every order line and
every purchase row links to an Item, and deleting one takes the history with
it. `set_item_enabled` is the retirement: ERPNext stops offering a disabled item
everywhere, and the past keeps its names.
"""

import frappe
from frappe import _

from luuvcrm import activity, roles
from luuvcrm.api.menu import (
    _clean_tags,
    _kitchen_group_labels,
    _set_pos_item_price,
    active_menu_name,
)
from luuvcrm.api.pricing import _get_pos_price_list, _resolve_item_rate

#: One page of the item master. The restaurant with the longest list this app
#: has seen has ~120; the ceiling is here so a site that grows a supplier
#: catalogue does not send it all to a phone.
ITEM_CEILING = 500


@frappe.whitelist()
def get_items(search=None, group=None, include_disabled=0):
    """The item master, with each item's POS price and whether it is on the menu."""
    roles.require_manager()
    filters = {}
    if str(include_disabled).strip().lower() in ("", "0", "false", "no"):
        filters["disabled"] = 0
    if group:
        filters["item_group"] = group

    or_filters = None
    if search:
        term = "%" + str(search).strip() + "%"
        or_filters = {"name": ["like", term], "item_name": ["like", term]}

    has_extras = [f for f in ("kitchen_group", "guide_commission_percent", "speciality_tags")
                  if frappe.db.has_column("Item", f)]
    rows = frappe.get_all(
        "Item", filters=filters, or_filters=or_filters,
        fields=["name", "item_name", "item_group", "image", "disabled",
                "is_stock_item", "stock_uom"] + has_extras,
        order_by="item_name asc", limit_page_length=ITEM_CEILING)

    menu = active_menu_name()
    on_menu = set()
    if menu:
        on_menu = set(frappe.get_all("Restaurant Menu Item", filters={"parent": menu},
                                     pluck="item", limit_page_length=0))
    price_list = _get_pos_price_list()
    labels = _kitchen_group_labels()

    items = []
    for r in rows:
        items.append({
            "code": r.name,
            "name": r.item_name or r.name,
            "group": r.item_group or "General",
            "image": r.image or "",
            "disabled": int(r.disabled or 0),
            "is_stock_item": int(r.is_stock_item or 0),
            "uom": r.stock_uom or "",
            "rate": _resolve_item_rate(r.name, 0, price_list),
            "on_menu": r.name in on_menu,
            "kitchen_group": r.get("kitchen_group") or "",
            "kitchen_group_label": labels.get(r.get("kitchen_group") or "", ""),
            "tags": r.get("speciality_tags") or "",
            "gd_pct": float(r.get("guide_commission_percent") or 0),
        })
    return {
        "items": items,
        "menu": menu or "",
        "groups": frappe.get_all("Item Group", filters={"is_group": 0},
                                 order_by="name asc", pluck="name", limit_page_length=0),
        "uoms": frappe.get_all("UOM", order_by="name asc", pluck="name", limit_page_length=60),
        "ceiling": ITEM_CEILING,
        "truncated": len(rows) >= ITEM_CEILING,
    }


@frappe.whitelist(methods=["POST"])
def create_item(item_name=None, rate=None, group=None, is_stock_item=0, uom=None):
    """Create an item without putting it on a menu.

    A purchase-only item (oil, gas, packaging) and an add-on both need to exist
    before anything can reference them, and neither belongs on the menu.
    """
    roles.require_manager()
    item_name = (item_name or "").strip()
    if not item_name:
        frappe.throw(_("Enter an item name"))
    group = (group or "").strip()
    if not group or not frappe.db.exists("Item Group", group):
        frappe.throw(_("Pick a valid category"))
    if frappe.db.exists("Item", item_name):
        frappe.throw(_("{0} already exists").format(item_name))

    stock = str(is_stock_item).strip().lower() not in ("", "0", "false", "no")
    doc = frappe.get_doc({
        "doctype": "Item",
        "item_code": item_name,
        "item_name": item_name,
        "item_group": group,
        "stock_uom": (uom or "").strip() or "Nos",
        "is_stock_item": 1 if stock else 0,
    })
    doc.insert(ignore_permissions=True)

    rate = float(rate or 0)
    if rate > 0:
        _set_pos_item_price(item_name, rate, _get_pos_price_list())

    activity.log_activity(
        "item.create", label="Item created: %s" % item_name,
        ref_doctype="Item", ref_name=item_name, item=item_name, item_name=item_name,
        amount=rate or None,
        detail={"group": group, "stock_item": bool(stock), "uom": doc.stock_uom})
    return {"ok": True, "code": item_name}


@frappe.whitelist(methods=["POST"])
def update_item(item_code=None, item_name=None, rate=None, group=None,
                kitchen_group=None, tags=None, guide_pct=None, description=None):
    """Edit the item itself. Works whether or not it is on a menu.

    `luuvcrm.api.menu.update_menu_item` is the menu-scoped editor and refuses an
    item that is not on the menu — which is right for a menu screen and useless
    for the item master, where most of the list is off the menu by definition.
    """
    roles.require_manager()
    item_code = (item_code or "").strip()
    if not item_code or not frappe.db.exists("Item", item_code):
        frappe.throw(_("Item not found"))

    price_list = _get_pos_price_list()
    before = float(_resolve_item_rate(item_code, 0, price_list) or 0)

    if item_name not in (None, ""):
        frappe.db.set_value("Item", item_code, "item_name", str(item_name).strip())
    if group not in (None, ""):
        if not frappe.db.exists("Item Group", str(group).strip()):
            frappe.throw(_("Pick a valid category"))
        frappe.db.set_value("Item", item_code, "item_group", str(group).strip())
    if rate not in (None, ""):
        rate = float(rate)
        if rate <= 0:
            frappe.throw(_("Price must be above zero"))
        _set_pos_item_price(item_code, rate, price_list)
        # Keep the menu row's fallback rate in step when it is on a menu, so the
        # two cannot disagree the day the price list is missing.
        menu = active_menu_name()
        if menu:
            row = frappe.db.get_value("Restaurant Menu Item",
                                      {"parent": menu, "item": item_code}, "name")
            if row:
                frappe.db.set_value("Restaurant Menu Item", row, "rate", rate)
    if kitchen_group is not None and frappe.db.has_column("Item", "kitchen_group"):
        kg = str(kitchen_group).strip()
        if kg and not frappe.db.exists("Kitchen Group", kg):
            frappe.throw(_("Pick a valid kitchen group"))
        frappe.db.set_value("Item", item_code, "kitchen_group", kg or None)
    if tags is not None and frappe.db.has_column("Item", "speciality_tags"):
        frappe.db.set_value("Item", item_code, "speciality_tags", _clean_tags(tags))
    if guide_pct is not None and frappe.db.has_column("Item", "guide_commission_percent"):
        gp = float(guide_pct or 0)
        if gp < 0 or gp > 50:
            frappe.throw(_("Guide commission must be between 0 and 50"))
        frappe.db.set_value("Item", item_code, "guide_commission_percent", gp)
    if description is not None:
        frappe.db.set_value("Item", item_code, "description", str(description).strip()[:2000])

    after = float(_resolve_item_rate(item_code, 0, price_list) or 0)
    activity.log_activity(
        "item.update", label="Item edited: %s" % item_code,
        ref_doctype="Item", ref_name=item_code, item=item_code,
        item_name=frappe.db.get_value("Item", item_code, "item_name") or item_code,
        amount=after - before, total_before=before, total_after=after,
        detail={"renamed_to": item_name or None, "group": group or None})
    return {"ok": True, "code": item_code, "rate": after}


@frappe.whitelist(methods=["POST"])
def set_item_enabled(item_code=None, enabled=1):
    """Retire an item, or bring it back. This is the closest thing to a delete
    that keeps the history: a disabled Item cannot be sold or bought anywhere in
    ERPNext, and every past bill still knows what it was called."""
    roles.require_manager()
    item_code = (item_code or "").strip()
    if not item_code or not frappe.db.exists("Item", item_code):
        frappe.throw(_("Item not found"))
    on = str(enabled).strip().lower() not in ("", "0", "false", "no")

    if not on:
        menu = active_menu_name()
        if menu and frappe.db.exists("Restaurant Menu Item",
                                     {"parent": menu, "item": item_code}):
            frappe.throw(_("{0} is on the menu — take it off the menu first, "
                           "so nobody wonders why it stopped appearing").format(item_code))

    frappe.db.set_value("Item", item_code, "disabled", 0 if on else 1)
    activity.log_activity(
        "item.enabled", label="%s %s" % (item_code, "back in use" if on else "retired"),
        ref_doctype="Item", ref_name=item_code, item=item_code,
        detail={"enabled": int(on)})
    return {"ok": True, "code": item_code, "enabled": int(on)}
