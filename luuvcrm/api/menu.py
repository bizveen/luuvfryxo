"""The menu itself: item detail, reviews, add-ons, and the manager's edit screen.

Split out of the single 7,338-line api.py; every name here is re-exported
from `luuvcrm.api`, so the whitelisted method paths are unchanged.
"""

import frappe
from frappe import _
from luuvcrm import activity, branch, roles

from luuvcrm.api.pricing import _get_pos_price_list, _resolve_item_rate

#: What upload_menu_image will store. Kept small and explicit — this endpoint is
#: reachable by every manager, and "any file, any size" is a file server.
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
MAX_IMAGE_BYTES = 5 * 1024 * 1024


# ─── The menu, read once ─────────────────────────────────
# Eight screens build the menu — /pos, /waiter, /kiosk, /order, /menu, /home,
# /cus and the POS's offline payload — and until this existed all eight had
# their own copy of the same loop, five of them byte-identical, each doing a
# full `frappe.get_doc("Item")` per row.
#
# The copies are the reason this function exists, not the N+1: an item marked
# unavailable has to disappear from *every* screen, and a rule written eight
# times is a rule that will hold in seven places and quietly fail in the
# eighth. One reader, one rule.

#: Item fields the menu screens read. `image` and `description` are Frappe's;
#: the rest are this app's custom fields, and are asked for only when the site
#: has them (a fresh site gets them from CUSTOM_FIELDS, an older one from a
#: patch, and in between the read must not throw).
_MENU_ITEM_FIELDS = ("item_name", "item_group", "image", "description")
_MENU_ITEM_EXTRAS = ("kitchen_group", "prep_time", "speciality_tags",
                     "youtube_url", "guide_commission_percent")


def _menu_extra_fields():
    return [f for f in _MENU_ITEM_EXTRAS if frappe.db.has_column("Item", f)]


def active_menu_name(restaurant=None):
    """The menu a screen should show, or None. Never raises: a public page with
    no session has no branch, and a blank menu is a better answer than a 500."""
    restaurant = restaurant or branch.viewing()
    if not restaurant:
        return None
    name = frappe.db.get_value("Restaurant", restaurant, "active_menu")
    if not name or not frappe.db.exists("Restaurant Menu", name):
        return None
    return name if frappe.db.get_value("Restaurant Menu", name, "enabled") else None


def menu_items(restaurant=None, include_unavailable=False, with_addons=False):
    """Every item on the active menu, priced, in three queries rather than one
    per row.

    `include_unavailable` is for the owner's editor, which has to show the item
    it is about to bring back. Every customer- and staff-facing screen leaves it
    off, which is what makes "sold out" mean the same thing on all of them.
    """
    menu = active_menu_name(restaurant)
    if not menu:
        return []

    rows = frappe.get_all("Restaurant Menu Item", filters={"parent": menu},
                          fields=_menu_row_fields(), order_by="idx asc",
                          limit_page_length=0)
    if not rows:
        return []

    codes = [r.item for r in rows if r.item]
    details = {}
    if codes:
        fields = ["name"] + list(_MENU_ITEM_FIELDS) + _menu_extra_fields()
        for d in frappe.get_all("Item", filters={"name": ["in", codes]},
                                fields=fields, limit_page_length=0):
            details[d.name] = d

    addon_map = _addon_map(codes) if with_addons else {}
    kitchen_labels = _kitchen_group_labels()
    price_list = _get_pos_price_list()

    items = []
    for row in rows:
        if not row.item:
            continue
        available = int(row.get("available", 1) if "available" in row else 1)
        if not available and not include_unavailable:
            continue
        d = details.get(row.item) or frappe._dict()
        entry = {
            "code": row.item,
            "name": d.get("item_name") or row.item,
            "description": d.get("description") or "",
            "image": d.get("image") or "",
            "rate": _resolve_item_rate(row.item, row.rate, price_list),
            "group": d.get("item_group") or "General",
            "available": available,
            "kitchen_group": d.get("kitchen_group") or "",
            "kitchen_group_label": kitchen_labels.get(d.get("kitchen_group") or "", ""),
            "prep_time": d.get("prep_time") or 0,
            "tags": d.get("speciality_tags") or "",
            "youtube": d.get("youtube_url") or "",
            "gd_pct": float(d.get("guide_commission_percent") or 0),
        }
        if with_addons:
            entry["addons"] = addon_map.get(row.item, [])
        items.append(entry)
    return items


def _menu_row_fields():
    fields = ["item", "rate", "name"]
    if frappe.db.has_column("Restaurant Menu Item", "available"):
        fields.append("available")
    return fields


def _kitchen_group_labels():
    """{name: what the kitchen calls it}. Kitchen Group names are hashes, so a
    screen that prints the name prints a hash."""
    if not frappe.db.exists("DocType", "Kitchen Group"):
        return {}
    return {g.name: (g.group_name or g.name)
            for g in frappe.get_all("Kitchen Group", fields=["name", "group_name"],
                                    limit_page_length=0)}


def _addon_map(codes=None):
    """{item: [addon item codes]} for the enabled POS Item Addon rows."""
    if not frappe.db.exists("DocType", "POS Item Addon"):
        return {}
    filters = {"enabled": 1}
    if codes:
        filters["item"] = ["in", list(codes)]
    out = {}
    for r in frappe.get_all("POS Item Addon", filters=filters,
                            fields=["item", "addon_item"], limit_page_length=0):
        out.setdefault(r.item, []).append(r.addon_item)
    return out


def menu_groups(restaurant=None, sort_by="rate", **kwargs):
    """`menu_items` folded into the [{name, items}] shape every screen renders.

    The sort is part of the contract: the screens sorted by (group, rate) and
    the public menu page by rate alone, and grouping an unsorted list silently
    produces the same category twice.
    """
    from itertools import groupby

    items = menu_items(restaurant, **kwargs)
    items.sort(key=lambda x: (x["group"], x[sort_by] if sort_by in x else 0))
    return [{"name": key, "items": list(group)}
            for key, group in groupby(items, key=lambda x: x["group"])]


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



def _active_menu_doc(restaurant=None):
    """The menu to edit. `restaurant` is what makes the owner screen work: with
    two branches `branch.current()` answers "the only one" and then None, which
    is the right refusal for a public page and useless for an editor."""
    restaurant_name = restaurant or branch.viewing()
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
def update_menu_item(item_code=None, item_name=None, rate=None, guide_pct=None,
                     group=None, kitchen_group=None, prep_time=None, tags=None,
                     youtube=None, description=None, available=None, restaurant=None):
    """Edit one menu item.

    Every argument is optional and `None` means "leave it alone", so the same
    endpoint serves the manager's two-field dialog and the owner's full editor
    without either of them being able to blank a field it does not show.
    """
    roles.require_manager()
    item_code = (item_code or "").strip()
    if not item_code or not frappe.db.exists("Item", item_code):
        frappe.throw(_("Item not found"))
    menu = _active_menu_doc(restaurant)
    menu_row = next((r for r in menu.items if r.item == item_code), None)
    if not menu_row:
        frappe.throw(_("{0} is not on the active menu").format(item_code))

    item_name = (item_name or "").strip()
    if item_name:
        frappe.db.set_value("Item", item_code, "item_name", item_name)

    price_list = _get_pos_price_list()
    before_rate = float(_resolve_item_rate(item_code, 0, price_list) or 0)
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

    moved_to = None
    if group not in (None, ""):
        group = str(group).strip()
        if not frappe.db.exists("Item Group", group):
            frappe.throw(_("Pick a valid category"))
        if group != frappe.db.get_value("Item", item_code, "item_group"):
            moved_to = group
        frappe.db.set_value("Item", item_code, "item_group", group)

    if kitchen_group is not None and frappe.db.has_column("Item", "kitchen_group"):
        kg = str(kitchen_group).strip()
        if kg and not frappe.db.exists("Kitchen Group", kg):
            frappe.throw(_("Pick a valid kitchen group"))
        # Blank is meaningful: it sends the line back to the default KOT section.
        frappe.db.set_value("Item", item_code, "kitchen_group", kg or None)

    if prep_time is not None and frappe.db.has_column("Item", "prep_time"):
        frappe.db.set_value("Item", item_code, "prep_time", max(0, int(float(prep_time or 0))))

    if tags is not None and frappe.db.has_column("Item", "speciality_tags"):
        frappe.db.set_value("Item", item_code, "speciality_tags", _clean_tags(tags))

    if youtube is not None and frappe.db.has_column("Item", "youtube_url"):
        frappe.db.set_value("Item", item_code, "youtube_url", str(youtube).strip()[:300])

    if description is not None:
        frappe.db.set_value("Item", item_code, "description", str(description).strip()[:2000])

    if available is not None:
        _set_available(menu_row.name, available)

    new_rate = _resolve_item_rate(item_code, 0, price_list)
    # A price change is the quietest money change in the app: nothing on a bill
    # records that the item used to cost something else.
    activity.log_activity(
        "menu.update", label="Menu item edited: %s" % item_code,
        ref_doctype="Item", ref_name=item_code, item=item_code,
        item_name=frappe.db.get_value("Item", item_code, "item_name") or item_code,
        amount=float(new_rate or 0) - before_rate,
        total_before=before_rate, total_after=float(new_rate or 0),
        detail={"renamed_to": item_name or None,
                "guide_commission": guide_pct if guide_pct is not None else None,
                "moved_to_category": moved_to,
                "kitchen_group": kitchen_group,
                "available": None if available is None else int(_truthy(available))})

    return {"ok": True, "code": item_code,
            "name": frappe.db.get_value("Item", item_code, "item_name") or item_code,
            "rate": new_rate}
# ─── The owner's menu editor ─────────────────────────────────────────────────
# /manager has had a two-field dialog (name, price) since the beginning. Every
# other thing a menu item *is* — which category it sits in, which section of the
# kitchen prints it, whether it is on tonight, what the customer sees — lived in
# the Desk, which is exactly the place a restaurant owner should not have to go.

def _truthy(v):
    return str(v).strip().lower() not in ("", "0", "false", "no", "none")


def _clean_tags(tags):
    """Comma-separated speciality tags, deduplicated, order kept. The kiosk and
    the public menu render these as chips, so a stray ", ," becomes a blank one."""
    if isinstance(tags, (list, tuple)):
        parts = [str(t) for t in tags]
    else:
        parts = str(tags or "").split(",")
    out = []
    for t in parts:
        t = t.strip()[:40]
        if t and t not in out:
            out.append(t)
    return ", ".join(out[:8])


def _set_available(row_name, available):
    if not frappe.db.has_column("Restaurant Menu Item", "available"):
        frappe.throw(_("This site has not run the menu availability patch yet"))
    frappe.db.set_value("Restaurant Menu Item", row_name, "available",
                        1 if _truthy(available) else 0)


def _append_menu_row(menu, item_code, rate):
    """Put one item on the menu without saving the parent.

    `Restaurant Menu.on_update` belongs to hospitality, and it rebuilds the
    menu's own Price List from scratch on every save: it **deletes every Item
    Price in that list** and re-inserts one per menu row. That is a great deal
    to do because somebody added a burger, and on a site whose Price List was
    not created by that controller — `restaurant_menu` unset, which is how this
    one is — `get_price_list()` tries to create a second Price List with the
    same name and the save throws `DuplicateEntryError`. That is why the
    manager's "Add item" button has been failing.

    Prices here are managed by `_set_pos_item_price` against the POS selling
    price list, which is the list `_resolve_item_rate` actually reads, so the
    rebuild is not something we want even when it works.
    """
    idx = frappe.db.sql("""select coalesce(max(idx), 0) from `tabRestaurant Menu Item`
                           where parent = %s""", (menu.name,))[0][0]
    row = frappe.get_doc({
        "doctype": "Restaurant Menu Item",
        "parent": menu.name,
        "parenttype": "Restaurant Menu",
        "parentfield": "items",
        "idx": int(idx) + 1,
        "item": item_code,
        "rate": rate,
    })
    row.insert(ignore_permissions=True)
    frappe.clear_cache(doctype="Restaurant Menu")
    return row


def _menu_row(menu, item_code):
    row = next((r for r in menu.items if r.item == item_code), None)
    if not row:
        frappe.throw(_("{0} is not on the active menu").format(item_code))
    return row


@frappe.whitelist()
def get_menu_admin(restaurant=None):
    """Everything the owner's menu screen needs, in one call.

    Includes items that are switched off — the editor is the one screen that has
    to show what it is about to bring back — and the items that exist but are on
    no menu, so putting one back is a tap rather than retyping it and getting a
    second Item with the same name.
    """
    roles.require_manager()
    restaurants = []
    for r in frappe.get_all("Restaurant", fields=["name", "active_menu"],
                            order_by="name asc", limit_page_length=0):
        restaurants.append({"name": r.name, "menu": r.active_menu or ""})
    # A manager sees the one branch they are pinned to; an owner sees them all
    # and picks. `can_choose` is what the screen shows the picker for.
    if not branch.can_choose():
        here = branch.viewing()
        restaurants = [r for r in restaurants if r["name"] == here] or restaurants

    chosen = restaurant or branch.viewing()
    menu = active_menu_name(chosen)
    items = menu_items(chosen, include_unavailable=True, with_addons=True)
    on_menu = {i["code"] for i in items}

    off_menu = []
    if menu:
        for d in frappe.get_all("Item", filters={"disabled": 0, "is_stock_item": 0},
                                fields=["name", "item_name", "item_group"],
                                order_by="item_name asc", limit_page_length=500):
            if d.name not in on_menu:
                off_menu.append({"code": d.name, "name": d.item_name or d.name,
                                 "group": d.item_group or "General"})

    # A Kitchen Group's name is a hash, and its `group_name` is what the kitchen
    # calls it. The picker sends the name and shows the label; sending the label
    # would set a link to a record that does not exist.
    # A Restaurant can have several menus — a lunch card, a season, last year's —
    # and `Restaurant.active_menu` is the one every screen reads. Listing them is
    # what makes that switch visible instead of a field in the Desk.
    menus = []
    if chosen:
        menus = [{"name": m.name, "enabled": int(m.enabled or 0),
                  "items": frappe.db.count("Restaurant Menu Item", {"parent": m.name})}
                 for m in frappe.get_all("Restaurant Menu",
                                         filters={"restaurant": chosen},
                                         fields=["name", "enabled"],
                                         order_by="name asc", limit_page_length=0)]

    kitchen_groups = []
    if frappe.db.exists("DocType", "Kitchen Group"):
        kitchen_groups = [
            {"name": g.name, "label": g.group_name or g.name}
            for g in frappe.get_all("Kitchen Group",
                                    fields=["name", "group_name", "display_order"],
                                    order_by="display_order asc, group_name asc",
                                    limit_page_length=0)]

    return {
        "restaurants": restaurants,
        "restaurant": chosen,
        "menu": menu or "",
        "menus": menus,
        "price_list": _get_pos_price_list() or "",
        "items": items,
        "off_menu": off_menu,
        "groups": sorted({i["group"] for i in items}),
        "all_groups": frappe.get_all("Item Group", filters={"is_group": 0},
                                     order_by="name asc", pluck="name",
                                     limit_page_length=0),
        "kitchen_groups": kitchen_groups,
        "can_toggle": bool(frappe.db.has_column("Restaurant Menu Item", "available")),
    }


@frappe.whitelist(methods=["POST"])
def set_active_menu(restaurant=None, menu=None):
    """Choose which of a restaurant's menus is the live one.

    `Restaurant.active_menu` is what every screen resolves through — the till,
    the kiosk, the public page and the offline payload all ask for it by way of
    `active_menu_name()`. Switching it swaps the whole card at once, which is the
    point: a lunch menu and a dinner menu are two Restaurant Menu records, not
    two hundred items being switched off one at a time.
    """
    roles.require_manager()
    restaurant = (restaurant or branch.current() or "").strip()
    if not restaurant or not frappe.db.exists("Restaurant", restaurant):
        frappe.throw(_("Pick a restaurant"))
    menu = (menu or "").strip()
    if not menu or not frappe.db.exists("Restaurant Menu", menu):
        frappe.throw(_("Pick a menu"))
    if frappe.db.get_value("Restaurant Menu", menu, "restaurant") != restaurant:
        # Otherwise a branch quietly starts selling another branch's card.
        frappe.throw(_("{0} belongs to another restaurant").format(menu))
    if not frappe.db.get_value("Restaurant Menu", menu, "enabled"):
        frappe.throw(_("{0} is disabled — enable it before making it the live menu").format(menu))

    was = frappe.db.get_value("Restaurant", restaurant, "active_menu")
    frappe.db.set_value("Restaurant", restaurant, "active_menu", menu)
    frappe.clear_cache(doctype="Restaurant")

    activity.log_activity(
        "menu.switch", label="%s now serves %s" % (restaurant, menu),
        ref_doctype="Restaurant", ref_name=restaurant,
        detail={"was": was, "now": menu})
    return {"ok": True, "restaurant": restaurant, "menu": menu, "was": was}


@frappe.whitelist(methods=["POST"])
def set_menu_item_available(item_code=None, available=1, restaurant=None):
    """The one-tap "sold out tonight". Presentation only: the item keeps its
    price and its history, and a bill that already carries it still prices and
    pays — hiding it from the ordering screens is the whole effect."""
    roles.require_manager()
    item_code = (item_code or "").strip()
    menu = _active_menu_doc(restaurant)
    row = _menu_row(menu, item_code)
    on = _truthy(available)
    _set_available(row.name, on)

    activity.log_activity(
        "menu.available",
        label="%s %s" % (item_code, "back on the menu" if on else "marked sold out"),
        ref_doctype="Item", ref_name=item_code, item=item_code,
        detail={"available": int(on), "menu": menu.name})
    return {"ok": True, "code": item_code, "available": int(on)}


@frappe.whitelist(methods=["POST"])
def remove_menu_item(item_code=None, restaurant=None):
    """Take an item off the menu. The Item itself is kept, always: every past
    invoice links to it, and deleting it would take the history with it. To stop
    selling something for one evening use set_menu_item_available instead — this
    is the one that forgets where it sat in the list."""
    roles.require_manager()
    item_code = (item_code or "").strip()
    menu = _active_menu_doc(restaurant)
    row = _menu_row(menu, item_code)
    frappe.db.delete("Restaurant Menu Item", {"name": row.name})
    frappe.clear_cache(doctype="Restaurant Menu")

    activity.log_activity(
        "menu.remove", label="Removed from the menu: %s" % item_code,
        ref_doctype="Item", ref_name=item_code, item=item_code,
        detail={"menu": menu.name, "rate": float(row.rate or 0)})
    return {"ok": True, "code": item_code}


@frappe.whitelist(methods=["POST"])
def restore_menu_item(item_code=None, rate=None, restaurant=None):
    """Put an existing Item back on the menu — the other half of remove, and the
    reason the editor lists what is off the menu. Retyping the name instead
    creates a second Item with the same name and splits the item's history."""
    roles.require_manager()
    item_code = (item_code or "").strip()
    if not item_code or not frappe.db.exists("Item", item_code):
        frappe.throw(_("Item not found"))
    menu = _active_menu_doc(restaurant)
    if any(r.item == item_code for r in menu.items):
        frappe.throw(_("{0} is already on the menu").format(item_code))

    price_list = _get_pos_price_list()
    resolved = float(rate or 0) or float(_resolve_item_rate(item_code, 0, price_list) or 0)
    _append_menu_row(menu, item_code, resolved)

    activity.log_activity(
        "menu.restore", label="Back on the menu: %s at Rs %.0f" % (item_code, resolved),
        ref_doctype="Item", ref_name=item_code, item=item_code, amount=resolved,
        detail={"menu": menu.name})
    return {"ok": True, "code": item_code, "rate": resolved}


@frappe.whitelist(methods=["POST"])
def set_item_addons(item_code=None, addons=None):
    """Replace the add-on set offered with one item.

    Rows are rewritten rather than edited because the set is what the owner is
    choosing; a diff would leave a disabled row behind and the POS reads enabled
    rows only, which is a difference nobody can see from the screen.
    """
    roles.require_manager()
    item_code = (item_code or "").strip()
    if not item_code or not frappe.db.exists("Item", item_code):
        frappe.throw(_("Item not found"))
    if not frappe.db.exists("DocType", "POS Item Addon"):
        frappe.throw(_("Add-ons are not installed on this site"))

    if isinstance(addons, str):
        addons = frappe.parse_json(addons) if addons.strip().startswith("[") \
            else [a.strip() for a in addons.split(",")]
    wanted = []
    for code in (addons or []):
        code = str(code).strip()
        if not code or code == item_code:      # an item cannot be its own add-on
            continue
        if not frappe.db.exists("Item", code):
            frappe.throw(_("Add-on item {0} does not exist").format(code))
        if code not in wanted:
            wanted.append(code)

    frappe.db.delete("POS Item Addon", {"item": item_code})
    for code in wanted:
        frappe.get_doc({"doctype": "POS Item Addon", "item": item_code,
                        "addon_item": code, "enabled": 1}).insert(ignore_permissions=True)

    activity.log_activity(
        "menu.addons", label="Add-ons for %s: %d" % (item_code, len(wanted)),
        ref_doctype="Item", ref_name=item_code, item=item_code, qty=len(wanted),
        detail={"addons": wanted})
    return {"ok": True, "code": item_code, "addons": wanted}


@frappe.whitelist(methods=["POST"])
def upload_menu_image(item_code=None):
    """Attach a photo to a menu item and make it the Item's image.

    Written here rather than pointing the screen at Frappe's own upload_file
    because that one attaches under the caller's Item permissions, and the roles
    that run this screen are not meant to hold blanket Item write in the Desk.
    The file is validated by type and size before it is stored: this endpoint is
    reachable by anyone who can edit the menu, and an upload path that accepts
    anything is a file server.
    """
    roles.require_manager()
    item_code = (item_code or frappe.form_dict.get("item_code") or "").strip()
    if not item_code or not frappe.db.exists("Item", item_code):
        frappe.throw(_("Item not found"))

    files = getattr(frappe.request, "files", None)
    upload = files.get("file") if files else None
    if not upload:
        frappe.throw(_("No file was sent"))

    content = upload.stream.read()
    if len(content) > MAX_IMAGE_BYTES:
        frappe.throw(_("Image must be under {0} MB").format(MAX_IMAGE_BYTES // (1024 * 1024)))
    filename = (upload.filename or "photo").rsplit("/", 1)[-1]
    extension = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if extension not in IMAGE_EXTENSIONS:
        frappe.throw(_("Use a JPG, PNG or WebP image"))

    from frappe.utils.file_manager import save_file

    saved = save_file(filename, content, "Item", item_code, is_private=0)
    frappe.db.set_value("Item", item_code, "image", saved.file_url)

    activity.log_activity(
        "menu.photo", label="Photo set on %s" % item_code,
        ref_doctype="Item", ref_name=item_code, item=item_code,
        detail={"file": saved.file_url, "bytes": len(content)})
    return {"ok": True, "code": item_code, "image": saved.file_url}


@frappe.whitelist(methods=["POST"])
def add_menu_item(item_name=None, rate=None, group=None, guide_pct=None,
                  kitchen_group=None, prep_time=None, tags=None, youtube=None,
                  description=None, restaurant=None):
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

    menu = _active_menu_doc(restaurant)
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

    # The presentation fields go on in one pass, so adding an item is one dialog
    # rather than "add it, then find it, then edit it".
    for field, value, column in (
            ("kitchen_group", kitchen_group, "kitchen_group"),
            ("prep_time", prep_time, "prep_time"),
            ("speciality_tags", tags, "speciality_tags"),
            ("youtube_url", youtube, "youtube_url")):
        if value in (None, "") or not frappe.db.has_column("Item", column):
            continue
        if field == "kitchen_group":
            if not frappe.db.exists("Kitchen Group", str(value).strip()):
                frappe.throw(_("Pick a valid kitchen group"))
            frappe.db.set_value("Item", item_code, field, str(value).strip())
        elif field == "prep_time":
            frappe.db.set_value("Item", item_code, field, max(0, int(float(value))))
        elif field == "speciality_tags":
            frappe.db.set_value("Item", item_code, field, _clean_tags(value))
        else:
            frappe.db.set_value("Item", item_code, field, str(value).strip()[:300])
    if description not in (None, ""):
        frappe.db.set_value("Item", item_code, "description", str(description).strip()[:2000])

    _append_menu_row(menu, item_code, rate)

    activity.log_activity(
        "menu.add", label="Menu item added: %s at Rs %.0f" % (item_name, rate),
        ref_doctype="Item", ref_name=item_code, item=item_code, item_name=item_name,
        amount=rate, total_after=rate,
        detail={"group": group, "guide_commission": guide_pct or None})

    return {"ok": True, "code": item_code, "name": item_name,
            "rate": _resolve_item_rate(item_code, rate, price_list), "group": group}
