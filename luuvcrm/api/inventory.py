"""BOM-backed restaurant inventory movements.

Purchases receive stock into the branch warehouse.  A menu item fired to the
kitchen consumes the stock items directly listed in its submitted default BOM.
Those lines may be raw materials or prepared stock (naan, cooked chicken,
sauces): preparing a batch consumes that item's own BOM and receives the
prepared item.  The final menu item itself is deliberately not manufactured:
restaurant food is made and served immediately, so its useful ledger movement
is a Material Issue of the ingredients actually taken from storage.

The integration is opt-in by data, not by a site-wide switch:

* purchase lines move only Items with ``is_stock_item`` enabled;
* menu lines move only when they have an active submitted default BOM (or are
  themselves a stock item, such as a bottled drink).

That makes the rollout safe for an existing restaurant while recipes and
opening balances are entered a few items at a time.
"""

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import flt, now_datetime, nowdate, nowtime

from luuvcrm import activity, branch, roles


CONSUMED_FIELD = "stock_consumed_qty"
ORDER_REF_FIELD = "luuv_pos_order"
PURCHASE_REF_FIELD = "luuv_purchase_bill"


def _active_restaurant(restaurant=None):
    """Inventory operations must use the restaurant on the caller's screen."""
    active = branch.viewing()
    if restaurant and restaurant != active:
        frappe.throw(_("Switch to that restaurant before managing its inventory"),
                     frappe.PermissionError)
    return active


def _has_field(doctype, fieldname):
    return bool(frappe.db.has_column(doctype, fieldname))


def _document_restaurant(doc):
    if doc.meta.has_field(branch.FIELD) and doc.get(branch.FIELD):
        return doc.get(branch.FIELD)
    if doc.meta.has_field("restaurant_table") and doc.get("restaurant_table"):
        return frappe.db.get_value("Restaurant Table", doc.get("restaurant_table"), "restaurant")
    return branch.current()


def _stock_context(doc):
    restaurant = _document_restaurant(doc)
    warehouse = branch.warehouse(restaurant)
    if not warehouse:
        frappe.throw(
            _("Set a stock Warehouse on Restaurant {0} before moving inventory").format(
                restaurant or ""
            )
        )
    row = frappe.db.get_value("Warehouse", warehouse, ["company", "is_group"], as_dict=True)
    if not row or row.is_group:
        frappe.throw(_("{0} is not a usable stock warehouse").format(warehouse))
    return restaurant, warehouse, row.company


def _new_stock_entry(purpose, company, warehouse, remarks, posting_date=None):
    entry = frappe.get_doc(
        {
            "doctype": "Stock Entry",
            "stock_entry_type": purpose,
            "purpose": purpose,
            "company": company,
            "from_warehouse": warehouse if purpose == "Material Issue" else None,
            "to_warehouse": warehouse if purpose == "Material Receipt" else None,
            "remarks": remarks,
            "items": [],
        }
    )
    if posting_date:
        entry.set_posting_time = 1
        entry.posting_date = posting_date
        entry.posting_time = now_datetime().strftime("%H:%M:%S")
    entry.flags.ignore_permissions = True
    return entry


def _submit(entry):
    entry.insert(ignore_permissions=True)
    entry.flags.ignore_permissions = True
    entry.submit()
    return entry


def _default_bom(item_code):
    return frappe.db.get_value(
        "BOM",
        {"item": item_code, "is_active": 1, "is_default": 1, "docstatus": 1},
        "name",
    )


def _components(item_code, qty, company):
    """Return immediate stock-UOM ingredients for ``qty`` menu units.

    A stock menu item without a BOM (a can or bottle) issues itself.  A
    made-to-order non-stock item without a BOM is intentionally skipped; this
    is how recipes can be rolled out without stopping every existing item.

    The BOM is deliberately *not* exploded.  A BOM row may be prepared stock
    made earlier (for example a naan or cooked chicken).  Its preparation has
    already consumed flour/chicken/spices, so exploding it again at sale time
    would leave the prepared stock untouched and charge the raw materials
    twice.
    """
    bom = _default_bom(item_code)
    if bom:
        from erpnext.manufacturing.doctype.bom.bom import get_bom_items_as_dict

        rows = get_bom_items_as_dict(bom, company, qty=qty, fetch_exploded=0)
        if not rows:
            frappe.throw(_("BOM {0} has no stock ingredients").format(bom))
        return bom, [
            {
                "item_code": code,
                "qty": flt(row.qty),
                "uom": row.stock_uom,
                "conversion_factor": 1,
            }
            for code, row in rows.items()
            if flt(row.qty) > 0
        ]

    item = frappe.db.get_value(
        "Item", item_code, ["is_stock_item", "stock_uom"], as_dict=True
    )
    if item and item.is_stock_item:
        return None, [
            {
                "item_code": item_code,
                "qty": flt(qty),
                "uom": item.stock_uom,
                "conversion_factor": 1,
            }
        ]
    return None, []


def consume_order_stock(order_name):
    """Issue ingredients for order quantities not processed before.

    ``stock_consumed_qty`` lives on each POS Order Item and records menu-item
    units, not ingredient units.  The order row is locked while the delta is
    read and written, so a retried/double KOT request cannot issue twice.
    """
    if not _has_field("POS Order Item", CONSUMED_FIELD):
        return {"entry": None, "consumed": 0, "missing_bom": [], "configured": False}

    frappe.db.sql("select name from `tabPOS Order` where name=%s for update", (order_name,))
    order = frappe.get_doc("POS Order", order_name)
    pending = []
    for line in order.get("items") or []:
        delta = flt(line.qty) - flt(line.get(CONSUMED_FIELD))
        if delta > 0:
            pending.append((line, delta))
    if not pending:
        return {"entry": None, "consumed": 0, "missing_bom": [], "configured": True}

    # Do not require a warehouse until at least one line is inventory-backed.
    restaurant = _document_restaurant(order)
    warehouse = branch.warehouse(restaurant)
    company = frappe.db.get_value("Warehouse", warehouse, "company") if warehouse else None

    configured = []
    missing = []
    components = defaultdict(float)
    component_uom = {}
    used_boms = set()
    for line, delta in pending:
        # A BOM is company-scoped. If a warehouse is absent we can still tell a
        # non-stock/no-BOM menu line is unconfigured without blocking service.
        bom = _default_bom(line.item)
        direct_stock = bool(frappe.db.get_value("Item", line.item, "is_stock_item"))
        if not bom and not direct_stock:
            missing.append(line.item)
            continue
        if not company:
            _stock_context(order)  # raises a precise configuration error
        bom, rows = _components(line.item, delta, company)
        if bom:
            used_boms.add(bom)
        for row in rows:
            components[row["item_code"]] += flt(row["qty"])
            component_uom[row["item_code"]] = row["uom"]
        configured.append((line, delta))

    if not configured:
        return {
            "entry": None,
            "consumed": 0,
            "missing_bom": sorted(set(missing)),
            "configured": True,
        }

    restaurant, warehouse, company = _stock_context(order)
    entry = _new_stock_entry(
        "Material Issue",
        company,
        warehouse,
        _("Kitchen consumption for POS Order {0}").format(order.name),
    )
    if _has_field("Stock Entry", ORDER_REF_FIELD):
        entry.set(ORDER_REF_FIELD, order.name)
    if branch.cost_center(restaurant):
        entry.cost_center = branch.cost_center(restaurant)
    for item_code, qty in sorted(components.items()):
        entry.append(
            "items",
            {
                "item_code": item_code,
                "s_warehouse": warehouse,
                "qty": qty,
                "uom": component_uom[item_code],
                "conversion_factor": 1,
                "cost_center": branch.cost_center(restaurant),
            },
        )
    _submit(entry)

    menu_units = 0
    for line, delta in configured:
        new_total = flt(line.get(CONSUMED_FIELD)) + delta
        frappe.db.set_value("POS Order Item", line.name, CONSUMED_FIELD, new_total)
        menu_units += delta

    activity.log_activity(
        "stock.consume",
        label="Kitchen stock issued for %s" % order.name,
        ref_doctype="POS Order",
        ref_name=order.name,
        qty=menu_units,
        detail={
            "stock_entry": entry.name,
            "warehouse": warehouse,
            "boms": sorted(used_boms),
            "missing_bom": sorted(set(missing)),
        },
    )
    return {
        "entry": entry.name,
        "consumed": menu_units,
        "missing_bom": sorted(set(missing)),
        "configured": True,
    }


def receive_purchase_stock(purchase_bill):
    """Receive stock-item lines from an approved custom Purchase Bill."""
    doc = (
        frappe.get_doc("Purchase Bill", purchase_bill)
        if isinstance(purchase_bill, str)
        else purchase_bill
    )
    # A deploy that has loaded Python but not run its migration must not create
    # an unreferenced receipt which a retry could duplicate.
    if not (
        _has_field("Purchase Bill", "stock_entry")
        and _has_field("Stock Entry", PURCHASE_REF_FIELD)
    ):
        return {"entry": None, "received": 0, "configured": False}

    stock_lines = []
    for line in doc.get("items") or []:
        item = frappe.db.get_value(
            "Item", line.item, ["is_stock_item", "stock_uom"], as_dict=True
        )
        if not item or not item.is_stock_item or flt(line.qty) <= 0:
            continue
        uom = line.uom or item.stock_uom
        conversion = 1.0
        if uom != item.stock_uom:
            from erpnext.stock.get_item_details import get_conversion_factor

            conversion = flt(get_conversion_factor(line.item, uom).get("conversion_factor"))
            if not conversion:
                frappe.throw(
                    _("No conversion from {0} to {1} for {2}").format(
                        uom, item.stock_uom, line.item
                    )
                )
        stock_lines.append((line, uom, conversion))

    if not stock_lines:
        return {"entry": None, "received": 0, "configured": True}

    if _has_field("Purchase Bill", "stock_entry") and doc.get("stock_entry"):
        return {"entry": doc.stock_entry, "received": 0, "configured": True}
    if _has_field("Stock Entry", PURCHASE_REF_FIELD):
        existing = frappe.db.get_value(
            "Stock Entry",
            {PURCHASE_REF_FIELD: doc.name, "docstatus": ["<", 2]},
            "name",
        )
        if existing:
            if _has_field("Purchase Bill", "stock_entry"):
                doc.db_set("stock_entry", existing)
            return {"entry": existing, "received": 0, "configured": True}

    restaurant, warehouse, company = _stock_context(doc)
    entry = _new_stock_entry(
        "Material Receipt",
        company,
        warehouse,
        _("Stock received from Purchase Bill {0}").format(doc.name),
        posting_date=doc.get("purchase_date"),
    )
    if _has_field("Stock Entry", PURCHASE_REF_FIELD):
        entry.set(PURCHASE_REF_FIELD, doc.name)
    if branch.cost_center(restaurant):
        entry.cost_center = branch.cost_center(restaurant)

    total_qty = 0
    for line, uom, conversion in stock_lines:
        entry.append(
            "items",
            {
                "item_code": line.item,
                "t_warehouse": warehouse,
                "qty": flt(line.qty),
                "uom": uom,
                "conversion_factor": conversion,
                # Purchase Bill price is per entered UOM; Stock Entry's basic
                # rate is per stock UOM.
                "basic_rate": flt(line.price) / conversion,
                "set_basic_rate_manually": 1,
                "cost_center": branch.cost_center(restaurant),
            },
        )
        total_qty += flt(line.qty) * conversion
    _submit(entry)
    if _has_field("Purchase Bill", "stock_entry"):
        doc.db_set("stock_entry", entry.name)
    return {"entry": entry.name, "received": total_qty, "configured": True}


def _company_for_recipe(restaurant=None):
    restaurant = _active_restaurant(restaurant)
    warehouse = branch.warehouse(restaurant)
    if not warehouse:
        frappe.throw(
            _("Set a stock Warehouse on Restaurant {0} before creating a BOM").format(
                restaurant or ""
            )
        )
    company = frappe.db.get_value("Warehouse", warehouse, "company") if warehouse else None
    if not company:
        frappe.throw(_("The branch Warehouse has no Company"))
    return company


def _recipe(item_code=None, restaurant=None):
    """Current default BOM plus stock ingredients offered by recipe editors."""
    restaurant = _active_restaurant(restaurant)
    item_code = (item_code or "").strip()
    if not item_code or not frappe.db.exists("Item", item_code):
        frappe.throw(_("Item not found"))
    bom_name = _default_bom(item_code)
    ingredients = []
    if bom_name:
        bom = frappe.get_doc("BOM", bom_name)
        ingredients = [
            {
                "item": row.item_code,
                "name": row.item_name or row.item_code,
                "qty": flt(row.qty),
                "uom": row.uom or row.stock_uom,
            }
            for row in bom.get("items") or []
        ]
    catalog = [
        {
            "item": row.name,
            "name": row.item_name or row.name,
            "uom": row.stock_uom,
            "group": row.item_group or "",
        }
        for row in frappe.get_all(
            "Item",
            filters={"disabled": 0, "is_stock_item": 1},
            fields=["name", "item_name", "stock_uom", "item_group"],
            order_by="item_group asc, item_name asc",
            limit_page_length=500,
        )
        if row.name != item_code
    ]
    return {"item": item_code, "bom": bom_name or "", "ingredients": ingredients, "catalog": catalog}


@frappe.whitelist()
def get_menu_recipe(item_code=None, restaurant=None):
    """Current default BOM plus stock ingredients offered by the admin editor."""
    roles.require_manager()
    return _recipe(item_code, restaurant)


def _prepared_item(item_code=None, restaurant=None):
    """Prepared-stock recipe plus the balance in this branch's warehouse."""
    item_code = (item_code or "").strip()
    item = frappe.db.get_value(
        "Item", item_code, ["item_name", "stock_uom", "is_stock_item"], as_dict=True
    )
    if not item or not item.is_stock_item:
        frappe.throw(_("Prepared food must be a stock item"))

    recipe = _recipe(item_code, restaurant)
    warehouse = branch.warehouse(restaurant or branch.viewing())
    balance = 0
    if warehouse:
        from erpnext.stock.utils import get_stock_balance

        balance = flt(get_stock_balance(item_code, warehouse) or 0)
    recipe.update(
        {
            "name": item.item_name or item_code,
            "uom": item.stock_uom,
            "warehouse": warehouse or "",
            "balance": balance,
        }
    )
    return recipe


@frappe.whitelist()
def get_prepared_item(item_code=None, restaurant=None):
    """Recipe and current balance shown by the prepared-batch editor."""
    roles.require_purchases()
    return _prepared_item(item_code, restaurant)


def _save_recipe(item_code=None, ingredients=None, restaurant=None):
    """Create and submit a new default BOM version after its caller authorizes."""
    item_code = (item_code or "").strip()
    if not item_code or not frappe.db.exists("Item", item_code):
        frappe.throw(_("Item not found"))
    rows = frappe.parse_json(ingredients) if isinstance(ingredients, str) else ingredients
    if not rows:
        frappe.throw(_("Add at least one recipe ingredient"))

    merged = defaultdict(float)
    for row in rows:
        code = (row.get("item") or "").strip()
        qty = flt(row.get("qty"))
        if not code or not frappe.db.exists("Item", code):
            frappe.throw(_("Pick a valid recipe ingredient"))
        if code == item_code:
            frappe.throw(_("A menu item cannot contain itself"))
        if not frappe.db.get_value("Item", code, "is_stock_item"):
            frappe.throw(_("{0} is not a stock item").format(code))
        if qty <= 0:
            frappe.throw(_("Ingredient quantity must be above zero"))
        merged[code] += qty

    company = _company_for_recipe(restaurant)
    bom = frappe.get_doc(
        {
            "doctype": "BOM",
            "item": item_code,
            "company": company,
            "quantity": 1,
            "is_active": 1,
            "is_default": 1,
            "rm_cost_as_per": "Valuation Rate",
            "items": [
                {
                    "item_code": code,
                    "qty": qty,
                    "uom": frappe.db.get_value("Item", code, "stock_uom"),
                    "conversion_factor": 1,
                    "include_item_in_manufacturing": 1,
                }
                for code, qty in merged.items()
            ],
        }
    )
    bom.flags.ignore_permissions = True
    bom.insert(ignore_permissions=True)
    bom.flags.ignore_permissions = True
    bom.submit()

    activity.log_activity(
        "bom.create",
        label="Recipe saved: %s" % item_code,
        ref_doctype="Item",
        ref_name=item_code,
        item=item_code,
        item_name=frappe.db.get_value("Item", item_code, "item_name") or item_code,
        detail={"bom": bom.name, "ingredients": dict(merged)},
    )
    return _recipe(item_code, restaurant)


@frappe.whitelist(methods=["POST"])
def save_menu_recipe(item_code=None, ingredients=None, restaurant=None):
    """Create and submit a new default BOM version for a menu item."""
    roles.require_manager()
    return _save_recipe(item_code, ingredients, restaurant)


@frappe.whitelist(methods=["POST"])
def save_prepared_recipe(item_code=None, ingredients=None, restaurant=None):
    """Save the production BOM for a stock item kept after preparation."""
    roles.require_purchases()
    item_code = (item_code or "").strip()
    if not frappe.db.get_value("Item", item_code, "is_stock_item"):
        frappe.throw(_("Prepared food must be a stock item"))
    _save_recipe(item_code, ingredients, restaurant)
    return _prepared_item(item_code, restaurant)


@frappe.whitelist()
def get_purchase_recipe_items(search=None, restaurant=None):
    """All active items a purchaser may add or update a BOM for."""
    roles.require_purchases()
    restaurant = _active_restaurant(restaurant)
    filters = {"disabled": 0}
    or_filters = None
    if search:
        term = "%" + str(search).strip() + "%"
        or_filters = {"name": ["like", term], "item_name": ["like", term]}
    rows = frappe.get_all(
        "Item",
        filters=filters,
        or_filters=or_filters,
        fields=["name", "item_name", "stock_uom", "is_stock_item", "item_group"],
        order_by="item_name asc",
        limit_page_length=500,
    )
    codes = [row.name for row in rows]
    bom_by_item = {}
    if codes:
        for row in frappe.get_all(
            "BOM",
            filters={
                "item": ["in", codes],
                "is_active": 1,
                "is_default": 1,
                "docstatus": 1,
            },
            fields=["name", "item"],
            limit_page_length=500,
        ):
            bom_by_item[row.item] = row.name
    return {
        "items": [
            {
                "item": row.name,
                "name": row.item_name or row.name,
                "uom": row.stock_uom or "",
                "group": row.item_group or "",
                "is_stock_item": int(row.is_stock_item or 0),
                "bom": bom_by_item.get(row.name, ""),
            }
            for row in rows
        ],
        "restaurant": restaurant or branch.viewing() or "",
    }


@frappe.whitelist()
def get_purchase_recipe(item_code=None, restaurant=None):
    """One BOM for the editor inside the purchases app."""
    roles.require_purchases()
    recipe = _recipe(item_code, restaurant)
    item = frappe.db.get_value(
        "Item", item_code, ["item_name", "stock_uom", "is_stock_item", "item_group"], as_dict=True
    )
    recipe.update(
        {
            "name": item.item_name or item_code,
            "uom": item.stock_uom or "",
            "group": item.item_group or "",
            "is_stock_item": int(item.is_stock_item or 0),
        }
    )
    if item.is_stock_item:
        prepared = _prepared_item(item_code, restaurant)
        recipe.update({"balance": prepared["balance"], "warehouse": prepared["warehouse"]})
    return recipe


@frappe.whitelist(methods=["POST"])
def save_purchase_recipe(item_code=None, ingredients=None, restaurant=None):
    """Add or update any item's BOM from the purchases app."""
    roles.require_purchases()
    _save_recipe(item_code, ingredients, restaurant)
    return get_purchase_recipe(item_code, restaurant)


@frappe.whitelist(methods=["POST"])
def prepare_item_batch(item_code=None, qty=None, restaurant=None):
    """Consume one BOM level and receive a batch of prepared stock.

    ``use_multi_level_bom`` stays off on purpose: if a prepared component is
    itself held in stock, this batch consumes that component rather than its
    original raw materials.  Each preparation step therefore mirrors what the
    kitchen physically took from storage.
    """
    roles.require_purchases()
    item_code = (item_code or "").strip()
    qty = flt(qty)
    if qty <= 0:
        frappe.throw(_("Prepared quantity must be above zero"))
    item = frappe.db.get_value(
        "Item", item_code, ["item_name", "stock_uom", "is_stock_item"], as_dict=True
    )
    if not item or not item.is_stock_item:
        frappe.throw(_("Prepared food must be a stock item"))
    bom = _default_bom(item_code)
    if not bom:
        frappe.throw(_("Save a preparation recipe before making a batch"))

    restaurant = _active_restaurant(restaurant)
    warehouse = branch.warehouse(restaurant)
    company = frappe.db.get_value("Warehouse", warehouse, "company") if warehouse else None
    if not warehouse or not company:
        frappe.throw(_("Set a stock Warehouse on the Restaurant before preparing a batch"))

    entry = frappe.get_doc(
        {
            "doctype": "Stock Entry",
            "stock_entry_type": "Manufacture",
            "purpose": "Manufacture",
            "company": company,
            "from_warehouse": warehouse,
            "to_warehouse": warehouse,
            "bom_no": bom,
            "fg_completed_qty": qty,
            "use_multi_level_bom": 0,
            "posting_date": nowdate(),
            "posting_time": nowtime(),
            "remarks": _("Prepared batch of {0}").format(item.item_name or item_code),
            "items": [],
        }
    )
    entry.flags.ignore_permissions = True
    entry.get_items()
    for row in entry.items:
        row.cost_center = branch.cost_center(restaurant)
    if not any(row.s_warehouse for row in entry.items):
        frappe.throw(_("Preparation BOM {0} has no ingredients").format(bom))
    if branch.cost_center(restaurant):
        entry.cost_center = branch.cost_center(restaurant)
    _submit(entry)

    activity.log_activity(
        "stock.prepare",
        label="Prepared %s %s of %s" % (qty, item.stock_uom, item.item_name or item_code),
        ref_doctype="Stock Entry",
        ref_name=entry.name,
        item=item_code,
        item_name=item.item_name or item_code,
        qty=qty,
        detail={"bom": bom, "warehouse": warehouse, "restaurant": restaurant},
    )
    result = _prepared_item(item_code, restaurant)
    result.update({"entry": entry.name, "prepared_qty": qty})
    return result
