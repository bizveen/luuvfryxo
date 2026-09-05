"""The menu itself: item detail, reviews, add-ons, and the manager's edit screen.

Split out of the single 7,338-line api.py; every name here is re-exported
from `luuvcrm.api`, so the whitelisted method paths are unchanged.
"""

import frappe
from frappe import _
from luuvcrm import roles

from luuvcrm.api.pricing import _get_pos_price_list, _resolve_item_rate


# ─── Item Info (for detail overlay) ──────────────────────

@frappe.whitelist(allow_guest=True)
def get_item_info(item_code):
    if not item_code:
        return {}
    doc = frappe.get_doc("Item", item_code)
    return {
        "item_name": doc.item_name,
        "description": doc.description or "",
        "image": doc.image or "",
        "standard_rate": doc.standard_rate or 0,
        "speciality_tags": doc.get("speciality_tags") or "",
        "youtube_url": doc.get("youtube_url") or "",
        "prep_time": doc.get("prep_time") or 0,
        "item_group": doc.item_group or "",
    }
# ─── Item Reviews ────────────────────────────────────────

@frappe.whitelist(allow_guest=True)
def get_item_reviews(item_code):
    if not item_code:
        return {"reviews": []}
    reviews = frappe.get_all("Item Review",
        filters={"item": item_code},
        fields=["customer_name", "rating", "comment", "creation"],
        order_by="creation desc",
        limit=20
    )
    avg_rating = 0
    if reviews:
        avg_rating = sum(r["rating"] or 0 for r in reviews) / len(reviews)
    return {"reviews": reviews, "avg_rating": round(avg_rating, 1), "count": len(reviews)}
@frappe.whitelist(allow_guest=True)
def submit_item_review():
    data = frappe.local.form_dict
    item = data.get("item", "").strip()
    customer_name = data.get("customer_name", "").strip()
    rating = int(data.get("rating", 0))
    comment = data.get("comment", "").strip()
    submitted_by = data.get("submitted_by", "").strip()

    if not item or not customer_name or not rating:
        return {"success": False, "error": "Item, name and rating required"}

    review = frappe.get_doc({
        "doctype": "Item Review",
        "item": item,
        "customer_name": customer_name,
        "rating": min(max(rating, 1), 5),
        "comment": comment,
        "submitted_by": submitted_by,
    })
    review.flags.ignore_permissions = True
    review.insert()
    return {"success": True, "name": review.name}
# ─── POS Item Add-ons (extra sauce, salad… offered when a menu item is added) ─

@frappe.whitelist()
def setup_item_addons():
    """Create the `POS Item Addon` mapping DocType (idempotent). One row = one
    add-on offered with a menu item; manage rows from Desk (/app/pos-item-addon).
    The POS reads enabled rows and pops an add-on picker when the item is added."""
    frappe.only_for("System Manager")
    if frappe.db.exists("DocType", "POS Item Addon"):
        return {"created": False, "exists": True}
    frappe.get_doc({
        "doctype": "DocType", "name": "POS Item Addon", "module": "Zeloura", "custom": 1,
        "naming_rule": "Random", "autoname": "hash", "track_changes": 1,
        "fields": [
            {"fieldname": "item", "label": "Menu Item", "fieldtype": "Link",
             "options": "Item", "reqd": 1, "in_list_view": 1, "in_standard_filter": 1},
            {"fieldname": "addon_item", "label": "Add-on Item", "fieldtype": "Link",
             "options": "Item", "reqd": 1, "in_list_view": 1},
            {"fieldname": "enabled", "label": "Enabled", "fieldtype": "Check",
             "default": "1", "in_list_view": 1},
        ],
        "permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
    }).insert(ignore_permissions=True)
    frappe.db.commit()
    return {"created": True}
# ─── Manager menu settings (/manager Settings tab: add/edit menu items) ──────
# Price writes go to Item Price in the POS selling price list because that is
# the authority in _resolve_item_rate — pos/app/kiosk all resolve through it,
# so an edit here shows up on the POS on its next page load.



def _active_menu_doc():
    restaurant_name = frappe.get_value("Restaurant", {}, "name")
    active = frappe.get_value("Restaurant", restaurant_name, "active_menu") if restaurant_name else None
    if not active:
        frappe.throw(_("No active menu is configured on the Restaurant"))
    return frappe.get_doc("Restaurant Menu", active)
def _winning_pos_price_row(item_code, price_list):
    """Name of the Item Price row _resolve_item_rate would pick from the POS
    price list today (same filters + ordering), or None."""
    if not price_list:
        return None
    rows = frappe.db.sql(
        """
        SELECT name FROM `tabItem Price`
        WHERE item_code = %(item_code)s AND selling = 1 AND price_list = %(price_list)s
          AND (customer IS NULL OR customer = '')
          AND (valid_from IS NULL OR valid_from <= %(today)s)
          AND (valid_upto IS NULL OR valid_upto >= %(today)s)
        ORDER BY COALESCE(valid_from, '1900-01-01') DESC, modified DESC
        LIMIT 1
        """,
        {"item_code": item_code, "price_list": price_list, "today": frappe.utils.today()})
    return rows[0][0] if rows else None
def _set_pos_item_price(item_code, rate, price_list):
    """Upsert the POS-visible selling price: update the currently-winning Item
    Price row in the POS price list (so promo/validity rows aren't shadowed by
    a new one that would lose the newest-window-wins ordering), else insert."""
    row = _winning_pos_price_row(item_code, price_list)
    if row:
        frappe.db.set_value("Item Price", row, "price_list_rate", rate)
    elif price_list:
        frappe.get_doc({
            "doctype": "Item Price",
            "item_code": item_code,
            "price_list": price_list,
            "selling": 1,
            "price_list_rate": rate,
        }).insert(ignore_permissions=True)
    else:
        # No POS selling price list configured — standard_rate is the last
        # fallback _resolve_item_rate reads.
        frappe.db.set_value("Item", item_code, "standard_rate", rate)
@frappe.whitelist()
def get_menu_settings():
    """Active-menu item list for the Manager Settings tab (resolved POS rates)."""
    roles.require_manager()
    menu = _active_menu_doc()
    price_list = _get_pos_price_list()
    has_gd = frappe.db.has_column("Item", "guide_commission_percent")
    items = []
    for row in menu.items:
        if not row.item:
            continue
        fields = ["item_name", "item_group"] + (["guide_commission_percent"] if has_gd else [])
        d = frappe.db.get_value("Item", row.item, fields, as_dict=True) or {}
        items.append({
            "code": row.item,
            "name": d.get("item_name") or row.item,
            "group": d.get("item_group") or "General",
            "rate": _resolve_item_rate(row.item, row.rate, price_list),
            "gd_pct": float(d.get("guide_commission_percent") or 0),
        })
    items.sort(key=lambda x: (x["group"], x["name"]))
    groups = sorted({i["group"] for i in items})
    all_groups = frappe.get_all("Item Group", filters={"is_group": 0}, order_by="name", pluck="name")
    return {"menu": menu.name, "price_list": price_list or "",
            "items": items, "groups": groups, "all_groups": all_groups}
@frappe.whitelist(methods=["POST"])
def update_menu_item(item_code=None, item_name=None, rate=None, guide_pct=None):
    """Rename, reprice and/or set the guide-commission override for a menu item."""
    roles.require_manager()
    item_code = (item_code or "").strip()
    if not item_code or not frappe.db.exists("Item", item_code):
        frappe.throw(_("Item not found"))
    menu = _active_menu_doc()
    menu_row = next((r for r in menu.items if r.item == item_code), None)
    if not menu_row:
        frappe.throw(_("{0} is not on the active menu").format(item_code))

    item_name = (item_name or "").strip()
    if item_name:
        frappe.db.set_value("Item", item_code, "item_name", item_name)

    price_list = _get_pos_price_list()
    if rate not in (None, ""):
        rate = float(rate)
        if rate <= 0:
            frappe.throw(_("Price must be above zero"))
        _set_pos_item_price(item_code, rate, price_list)
        # keep the menu row's fallback rate in step (no doc save → no side effects)
        frappe.db.set_value("Restaurant Menu Item", menu_row.name, "rate", rate)

    # 0 / blank clears the override → the guide's own percent applies again
    if guide_pct is not None and frappe.db.has_column("Item", "guide_commission_percent"):
        gp = float(guide_pct or 0)
        if gp < 0 or gp > 50:
            frappe.throw(_("Guide commission must be between 0 and 50"))
        frappe.db.set_value("Item", item_code, "guide_commission_percent", gp)

    return {"ok": True, "code": item_code,
            "name": frappe.db.get_value("Item", item_code, "item_name") or item_code,
            "rate": _resolve_item_rate(item_code, 0, price_list)}
@frappe.whitelist(methods=["POST"])
def add_menu_item(item_name=None, rate=None, group=None, guide_pct=None):
    """Create an Item (non-stock), price it, and append it to the active menu."""
    roles.require_manager()
    item_name = (item_name or "").strip()
    if not item_name:
        frappe.throw(_("Enter an item name"))
    rate = float(rate or 0)
    if rate <= 0:
        frappe.throw(_("Price must be above zero"))
    group = (group or "").strip()
    if not group or not frappe.db.exists("Item Group", group):
        frappe.throw(_("Pick a valid category"))

    menu = _active_menu_doc()
    item_code = item_name  # menu items here use the name as the code (matches existing items)
    if frappe.db.exists("Item", item_code):
        if any(r.item == item_code for r in menu.items):
            frappe.throw(_("{0} is already on the menu").format(item_code))
    else:
        frappe.get_doc({
            "doctype": "Item",
            "item_code": item_code,
            "item_name": item_name,
            "item_group": group,
            "stock_uom": "Nos",
            "is_stock_item": 0,
        }).insert(ignore_permissions=True)

    price_list = _get_pos_price_list()
    _set_pos_item_price(item_code, rate, price_list)

    if guide_pct not in (None, "") and frappe.db.has_column("Item", "guide_commission_percent"):
        gp = float(guide_pct or 0)
        if gp < 0 or gp > 50:
            frappe.throw(_("Guide commission must be between 0 and 50"))
        frappe.db.set_value("Item", item_code, "guide_commission_percent", gp)

    menu.append("items", {"item": item_code, "rate": rate})
    menu.save(ignore_permissions=True)

    return {"ok": True, "code": item_code, "name": item_name,
            "rate": _resolve_item_rate(item_code, rate, price_list), "group": group}
