"""Branch stock, immutable menu-recipe versions and counted-stock corrections."""
import math

import frappe
from frappe import _
from frappe.utils import flt, get_datetime, now_datetime, nowdate, nowtime

from luuvcrm import activity, branch, roles
from luuvcrm.api import inventory
from luuvcrm.api.menu import active_menu_name


def _context(restaurant=None):
    roles.require_manager()
    active = inventory._active_restaurant(restaurant)
    if not active:
        frappe.throw(_("Select your branch first"))
    warehouse = branch.warehouse(active)
    if not warehouse:
        frappe.throw(_("Set up the branch stock warehouse first"))
    return active, warehouse


def _menu(restaurant):
    menu = active_menu_name(restaurant)
    return frappe.get_all("Restaurant Menu Item", filters={"parent": menu},
                          pluck="item", limit_page_length=0) if menu else []


def _check_item(item, restaurant):
    if item not in _menu(restaurant):
        frappe.throw(_("Choose an item on this branch's menu"), frappe.PermissionError)


def _versions(item, restaurant):
    return frappe.get_all("BOM", filters={"item": item, "luuv_recipe_restaurant": restaurant,
                                         "docstatus": ["<", 2]},
                          fields=["name", "docstatus", "creation", "owner", "luuv_recipe_effective_from"],
                          order_by="creation desc", limit_page_length=0)


def order_bom(item, restaurant, created):
    """Resolve at order creation time, never by today's mutable default BOM.

    None means no managed recipe was effective for this order. Legacy menus
    continue to use their existing default until they adopt managed versions.
    """
    if not frappe.db.has_column("BOM", "luuv_recipe_restaurant"):
        return inventory._default_bom(item)
    published = frappe.get_all("BOM", filters={"item": item,
        "luuv_recipe_restaurant": restaurant, "docstatus": 1},
        fields=["name", "luuv_recipe_effective_from"],
        order_by="luuv_recipe_effective_from desc", limit_page_length=0)
    if not published:
        return inventory._default_bom(item)
    for row in published:
        if row.luuv_recipe_effective_from and get_datetime(row.luuv_recipe_effective_from) <= get_datetime(created):
            return row.name
    return inventory._default_bom(item)


def _display(uom):
    key = (uom or "").lower()
    if key in ("kg", "kilogram"):
        return "g", 1000.0
    if key in ("l", "litre", "liter"):
        return "ml", 1000.0
    return uom, 1.0


def _catalog(warehouse, include_equipment=False):
    from luuvcrm.api.inventory_sections import main_items
    equipment = main_items(warehouse)
    minimums = frappe.parse_json(frappe.db.get_value("Warehouse", warehouse, "luuv_stock_minimums") or "{}") if frappe.db.has_column("Warehouse", "luuv_stock_minimums") else {}
    balances = {r.item_code: r for r in frappe.get_all(
        "Bin", filters={"warehouse": warehouse}, fields=["item_code", "actual_qty", "valuation_rate"], limit_page_length=0)}
    result = []
    for r in frappe.get_all("Item", filters={"disabled": 0, "is_stock_item": 1},
                            fields=["name", "item_name", "stock_uom"], order_by="item_name asc", limit_page_length=0):
        if not include_equipment and r.name in equipment:
            continue
        uom, factor = _display(r.stock_uom)
        result.append({"item": r.name, "name": r.item_name, "stock_uom": r.stock_uom,
                       "uom": uom, "factor": factor, "balance": flt(balances.get(r.name, {}).get("actual_qty", 0)),
                       "rate": flt(balances.get(r.name, {}).get("valuation_rate", 0)),
                       "minimum_qty": flt(minimums.get(r.name, 0))})
    return result


@frappe.whitelist()
def dashboard(restaurant=None):
    restaurant, warehouse = _context(restaurant)
    codes = _menu(restaurant)
    items = frappe.get_all("Item", filters={"name": ["in", codes], "disabled": 0},
                           fields=["name", "item_name"], order_by="item_name asc", limit_page_length=0) if codes else []
    managed = frappe.get_all("BOM", filters={"luuv_recipe_restaurant": restaurant, "docstatus": ["<", 2]},
                             fields=["name", "item", "docstatus"], order_by="creation desc", limit_page_length=0)
    recipe_items = set()
    if managed:
        recipe_items = set(frappe.get_all("BOM Item", filters={"parent": ["in", [r.name for r in managed]]},
                                          pluck="item_code", limit_page_length=0))
    bins = set(frappe.get_all("Bin", filters={"warehouse": warehouse}, pluck="item_code", limit_page_length=0))
    registered = set(frappe.parse_json(frappe.db.get_value("Warehouse", warehouse, "luuv_stock_minimums") or "{}")) if frappe.db.has_column("Warehouse", "luuv_stock_minimums") else set()
    from luuvcrm.api.inventory_sections import main_items, view
    all_stock = _catalog(warehouse, include_equipment=True)
    equipment = main_items(warehouse)
    catalog = [r for r in all_stock if r['item'] not in equipment]
    from luuvcrm.api.inventory_purchasing import pending_quantities, prepared_codes
    pending = pending_quantities(restaurant, catalog)
    prepared = prepared_codes(restaurant)
    for row in catalog:
        row['pending_qty'] = pending.get(row['item'], 0)
        row['prepared'] = row['item'] in prepared
    for item in items:
        versions = [v for v in managed if v.item == item.name]
        item["recipe_status"] = ("Active" if versions[0].docstatus == 1 else "Draft") if versions else (
            "Active" if inventory._default_bom(item.name) else "No recipe")
    audits = frappe.get_all("Stock Reconciliation", filters={"luuv_audit_restaurant": restaurant, "docstatus": 1},
                            fields=["name", "creation", "owner", "luuv_audit_details"],
                            order_by="creation desc", limit_page_length=25)
    for audit in audits:
        audit["details"] = frappe.parse_json(audit.pop("luuv_audit_details") or "{}")
    from luuvcrm.api.branch_stock_policy import current as stock_rules
    shortages=[]
    if frappe.db.has_column('Stock Entry','luuv_stock_policy_snapshot'):
        for entry in frappe.get_all('Stock Entry',filters={'from_warehouse':warehouse,'purpose':'Material Issue','docstatus':1},
            fields=['name','creation','luuv_pos_order','luuv_stock_policy_snapshot'],order_by='creation desc',limit_page_length=25):
            snapshot=frappe.parse_json(entry.luuv_stock_policy_snapshot or '{}')
            if snapshot.get('warnings'):shortages.append(dict(order=entry.luuv_pos_order,creation=entry.creation,**snapshot))
    return {**view(restaurant, warehouse, all_stock),
            "restaurant": restaurant, "warehouse": warehouse, "as_of": now_datetime(), "menu": items,
            "stock_policy": stock_rules(restaurant), "shortage_sales": shortages,
            "stock": [r for r in catalog if r["item"] in recipe_items | bins | registered], "audits": audits,
            "purchasing": True, "purchasing_flow": "bill_approval",
            "uoms": frappe.get_all("UOM", filters={"enabled": 1}, order_by="name asc", pluck="name", limit_page_length=0)}


@frappe.whitelist()
def recipe(item_code, restaurant=None, version=None):
    restaurant, warehouse = _context(restaurant)
    _check_item(item_code, restaurant)
    versions = _versions(item_code, restaurant)
    latest = versions[0].name if versions else inventory._default_bom(item_code) or ""
    selected = version or latest
    if version and version not in [v.name for v in versions] and version != latest:
        frappe.throw(_("Recipe version not found"), frappe.PermissionError)
    rows, status = [], "No recipe"
    if selected:
        doc = frappe.get_doc("BOM", selected)
        status = "Active" if doc.docstatus == 1 else "Draft"
        for line in doc.items:
            uom, factor = _display(line.stock_uom)
            rows.append({"item": line.item_code, "name": line.item_name,
                         "qty": flt(line.stock_qty) / flt(doc.quantity or 1) * factor, "uom": uom})
    return {"item": item_code, "name": frappe.db.get_value("Item", item_code, "item_name"),
            "latest": latest, "version": selected, "status": status, "ingredients": rows,
            "versions": versions, "catalog": _catalog(warehouse), "restaurant": restaurant}


@frappe.whitelist(methods=['POST'])
def move_inventory_item(item_code, section, expected_revision, category='', comment='', restaurant=None):
    from luuvcrm.api.inventory_sections import move
    return move(item_code, section, expected_revision, category, comment, restaurant)


@frappe.whitelist(methods=['POST'])
def inventory_item_units(item_code, restaurant=None):
    from luuvcrm.api import inventory_units
    restaurant, warehouse = _context(restaurant)
    visible = dashboard(restaurant)
    if item_code not in {r['item'] for r in visible['stock'] + visible['main_inventory']}:
        frappe.throw(_('Choose an item in this branch inventory.'), frappe.PermissionError)
    return inventory_units.describe(item_code)


@frappe.whitelist(methods=['POST'])
def rename_inventory_item(item_code, item_name, expected_name, restaurant=None, unit=None, expected_unit=None):
    """Edit the display name and, before recorded use, its base unit."""
    restaurant, warehouse = _context(restaurant)
    if not isinstance(item_name, str):
        frappe.throw(_("Enter an item name."))
    name = item_name.strip()
    if not 2 <= len(name) <= 140 or any(ord(c) < 32 or ord(c) == 127 or c in '<>' for c in name):
        frappe.throw(_("Use a plain-text item name of 2–140 characters."))
    visible = dashboard(restaurant)
    allowed = {r['item'] for r in visible['stock'] + visible['main_inventory']}
    if item_code not in allowed:
        frappe.throw(_("Choose an item in this branch inventory."), frappe.PermissionError)
    item = frappe.get_doc('Item', item_code, for_update=True)
    if item.disabled:
        frappe.throw(_("Choose an enabled item."))
    before = item.item_name
    if name == before and (unit is None or unit == item.stock_uom):
        return dict(ok=True, changed=False, item=item_code, name=name, unit=item.stock_uom)
    if expected_name != before:
        frappe.throw(_("This item name changed on another screen. Reopen it before saving."))
    if frappe.db.exists('Item', {'item_name': name, 'name': ['!=', item_code]}):
        frappe.throw(_("Another item already uses that name. Choose a different name."))
    unit_changed = False
    if unit is not None:
        from luuvcrm.api import inventory_units
        unit_changed = inventory_units.change(item, unit, expected_unit)
    if name == before:
        return dict(ok=True, changed=unit_changed, item=item_code, name=name, unit=item.stock_uom)
    # Do not use rename_doc or Item.save: neither linked IDs nor historical
    # child-row labels should be rewritten by an inventory name correction.
    frappe.db.set_value('Item', item_code, 'item_name', name)
    record = dict(item=item_code, restaurant=restaurant, before=before, after=name)
    frappe.get_doc(dict(doctype='Version', ref_doctype='Item', docname=item_code,
        data=frappe.as_json({'changed': [['item_name', before, name]],
                            'inventory_name_change': record}))).insert(ignore_permissions=True)
    activity.log_activity('item.name', label='Item name changed: '+name,
        ref_doctype='Item', ref_name=item_code, item=item_code, item_name=name, detail=record)
    frappe.clear_document_cache('Item', item_code)
    return dict(ok=True, changed=True, item=item_code, name=name, unit=item.stock_uom)


def _number(value, label, allow_zero=False):
    try:
        number = float(value)
    except (ValueError, TypeError):
        number = float("nan")
    if not math.isfinite(number) or number < 0 or (number == 0 and not allow_zero):
        frappe.throw(_("{0} must be a valid {1} number").format(label, "non-negative" if allow_zero else "positive"))
    return number


@frappe.whitelist(methods=['POST'])
def save_stock_policy(policy, restaurant=None):
    from luuvcrm.api.branch_stock_policy import save
    return save(policy, restaurant)


@frappe.whitelist(methods=["POST"])
def set_minimum(item_code, minimum_qty, restaurant=None):
    """Visual minimum stock threshold only; does not create purchase orders."""
    restaurant, warehouse = _context(restaurant)
    item = frappe.db.get_value("Item", item_code, ["is_stock_item", "disabled", "stock_uom"], as_dict=True)
    if not item or not item.is_stock_item or item.disabled:
        frappe.throw(_("Choose an enabled stock item"))
    minimum = _number(minimum_qty, "Minimum stock level", True)
    if frappe.db.get_value("UOM", item.stock_uom, "must_be_whole_number") and minimum != int(minimum):
        frappe.throw(_("Enter a whole number of {0}").format(item.stock_uom))
    frappe.db.sql("select name from tabWarehouse where name=%s for update", warehouse)
    values = frappe.parse_json(frappe.db.get_value("Warehouse", warehouse, "luuv_stock_minimums") or "{}")
    before = flt(values.get(item_code, 0))
    values[item_code] = minimum
    frappe.db.set_value("Warehouse", warehouse, "luuv_stock_minimums", frappe.as_json(values))
    activity.log_activity("stock.minimum", label="Minimum stock level updated: " + item_code,
        ref_doctype="Item", ref_name=item_code, item=item_code,
        detail={"restaurant": restaurant, "warehouse": warehouse, "before": before, "minimum_qty": minimum, "uom": item.stock_uom})
    return {"item": item_code, "minimum_qty": minimum, "uom": item.stock_uom}


@frappe.whitelist(methods=["POST"])
def ingredient_options(restaurant=None):
    """Existing stock can be registered in a branch before it has a Bin."""
    restaurant, warehouse = _context(restaurant)
    return {"items": _catalog(warehouse)}


@frappe.whitelist(methods=["POST"])
def create_ingredient(item_name, uom, minimum_qty=0, restaurant=None, item_code=None):
    restaurant, warehouse = _context(restaurant)
    name = (item_name or "").strip()
    if not name:
        frappe.throw(_("Enter an ingredient name or choose an existing item."))
    # Lock the branch registration and use a current read for name matching.
    # Retrying an add must never reset the existing minimum or stock balance.
    frappe.db.sql("select name from tabWarehouse where name=%s for update", warehouse)
    if item_code:
        matches = frappe.db.sql("select name from tabItem where name=%s for update", (item_code,), as_dict=True)
        if not matches:
            frappe.throw(_("This item is no longer available. Reopen Add ingredient."))
    else:
        matches = frappe.db.sql("select name from tabItem where item_name=%s or name=%s order by name for update",
                                (name, name), as_dict=True)
    if len(matches) > 1:
        frappe.throw(_("More than one item has this name. Choose the item code from Existing items."))
    if matches:
        doc = frappe.get_doc("Item", matches[0].name)
        if doc.disabled:
            frappe.throw(_("This item is disabled. Enable the existing item before adding it."))
        from luuvcrm.api.inventory_sections import main_items
        if doc.is_fixed_asset or doc.name in main_items(warehouse):
            frappe.throw(_("This item belongs to Main inventory. Manage it there or move it to Running stock first."))
        if not doc.is_stock_item:
            frappe.throw(_("This item is set up without stock tracking. Review its item setup before using it as an ingredient."))
        values = frappe.parse_json(frappe.db.get_value("Warehouse", warehouse, "luuv_stock_minimums") or "{}")
        registered = doc.name in values
        if not registered:
            # Existing units and thresholds are never reinterpreted from a
            # possibly stale new-item form. MOQ can be edited on the stock card.
            set_minimum(doc.name, 0, restaurant)
            activity.log_activity("item.register", label="Existing ingredient added: " + doc.item_name,
                ref_doctype="Item", ref_name=doc.name, item=doc.name, item_name=doc.item_name,
                detail={"restaurant": restaurant, "warehouse": warehouse, "unit": doc.stock_uom})
        return {"item": doc.name, "name": doc.item_name, "stock_uom": doc.stock_uom,
                "minimum_qty": flt(values.get(doc.name, 0)), "existed": True,
                "already_registered": registered}
    if not 2 <= len(name) <= 80:
        frappe.throw(_("Enter an ingredient name (2–80 characters)"))
    from luuvcrm.api.inventory_units import validate_unit
    validate_unit(uom)
    minimum = _number(minimum_qty, "Minimum stock level", True)
    if frappe.db.get_value("UOM", uom, "must_be_whole_number") and minimum != int(minimum):
        frappe.throw(_("Enter a whole number of {0}").format(uom))
    group = "Raw Materials"
    if not frappe.db.exists("Item Group", group):
        frappe.throw(_("Set up the Raw Materials item group first"))
    doc = frappe.get_doc({"doctype": "Item", "item_code": "LF-ING-" + frappe.generate_hash(length=10),
        "item_name": name, "item_group": group, "stock_uom": uom,
        "is_stock_item": 1, "is_purchase_item": 1, "is_sales_item": 0})
    doc.insert(ignore_permissions=True)
    set_minimum(doc.name, minimum, restaurant)
    activity.log_activity("item.create", label="Ingredient created: " + name, ref_doctype="Item",
        ref_name=doc.name, item=doc.name, item_name=name,
        detail={"restaurant": restaurant, "unit": uom, "minimum_qty": minimum})
    return {"item": doc.name, "name": name, "stock_uom": uom, "minimum_qty": minimum, "existed": False}


@frappe.whitelist(methods=["POST"])
def save_recipe(item_code, ingredients, expected_version="", restaurant=None):
    restaurant, warehouse = _context(restaurant)
    _check_item(item_code, restaurant)
    frappe.db.sql("select name from tabItem where name=%s for update", item_code)
    current = recipe(item_code, restaurant)
    if (expected_version or "") != current["latest"]:
        frappe.throw(_("This recipe changed on another screen. Reload it before saving."))
    rows = frappe.parse_json(ingredients) if isinstance(ingredients, str) else ingredients
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        frappe.throw(_("Add between 1 and 100 ingredients"))
    catalog = {r["item"]: r for r in current["catalog"]}
    merged = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("item") not in catalog or row.get("item") == item_code:
            frappe.throw(_("Choose a valid stock ingredient"))
        item = row["item"]
        qty = _number(row.get("qty"), "Ingredient quantity") / catalog[item]["factor"]
        merged[item] = merged.get(item, 0) + qty
    doc = frappe.get_doc({"doctype": "BOM", "item": item_code,
        "company": frappe.db.get_value("Warehouse", warehouse, "company"), "quantity": 1,
        "is_active": 1, "is_default": 0, "rm_cost_as_per": "Valuation Rate",
        "luuv_recipe_restaurant": restaurant,
        "items": [{"item_code": code, "qty": qty, "uom": catalog[code]["stock_uom"],
                   "conversion_factor": 1, "include_item_in_manufacturing": 1} for code, qty in merged.items()]})
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    activity.log_activity("recipe.draft", label="Recipe draft saved: " + item_code,
        ref_doctype="BOM", ref_name=doc.name, item=item_code,
        detail={"previous_version": current["latest"], "restaurant": restaurant, "ingredients": merged})
    return recipe(item_code, restaurant)


@frappe.whitelist(methods=["POST"])
def activate_recipe(item_code, version, restaurant=None):
    restaurant, warehouse = _context(restaurant)
    _check_item(item_code, restaurant)
    frappe.db.sql("select name from tabItem where name=%s for update", item_code)
    current = recipe(item_code, restaurant)
    if current["latest"] != version or current["status"] != "Draft":
        frappe.throw(_("Reload the latest draft before activating"))
    doc = frappe.get_doc("BOM", version)
    balances = {r["item"]: r["balance"] for r in current["catalog"]}
    from luuvcrm.api.branch_stock_policy import current as stock_rules
    from luuvcrm.stock_policy import assess
    checked=assess(stock_rules(restaurant),[dict(item=r.item_code,name=r.item_name,uom=r.stock_uom,
        before=balances.get(r.item_code,0),qty=flt(r.stock_qty)) for r in doc.items])
    missing = [r['name'] for r in checked['blocked']]
    if missing:
        frappe.throw(_("Record opening stock or a receipt before activating: {0}").format(", ".join(missing)))
    # Keep the global/default recipe untouched: this version belongs to a branch.
    old_default = frappe.db.get_value("Item", item_code, "default_bom")
    defaults = frappe.get_all("BOM", filters={"item": item_code, "is_default": 1}, pluck="name")
    doc.luuv_recipe_effective_from = now_datetime()
    doc.flags.ignore_permissions = True
    doc.submit()
    frappe.db.set_value("BOM", doc.name, "is_default", 0, update_modified=False)
    for name in defaults:
        frappe.db.set_value("BOM", name, "is_default", 1, update_modified=False)
    frappe.db.set_value("Item", item_code, "default_bom", old_default, update_modified=False)
    activity.log_activity("recipe.activate", label="Recipe activated: " + item_code,
        ref_doctype="BOM", ref_name=doc.name, item=item_code,
        detail={"restaurant": restaurant, "effective_from": str(doc.luuv_recipe_effective_from)})
    return recipe(item_code, restaurant)


@frappe.whitelist(methods=["POST"])
def correct_stock(item_code, counted_qty, expected_qty, comment, valuation_rate=None, restaurant=None):
    restaurant, warehouse = _context(restaurant)
    comment = (comment or "").strip()
    if not 3 <= len(comment) <= 1000:
        frappe.throw(_("Add a reason for this correction (3–1000 characters)"))
    item = frappe.db.get_value("Item", item_code, ["item_name", "stock_uom", "is_stock_item", "disabled", "has_batch_no", "has_serial_no"], as_dict=True)
    if not item or not item.is_stock_item or item.disabled:
        frappe.throw(_("Choose an enabled stock ingredient"))
    if item.has_batch_no or item.has_serial_no:
        frappe.throw(_("Use the stock reconciliation screen for batch or serial tracked items"))
    counted = _number(counted_qty, "Counted quantity", True)
    if frappe.db.get_value("UOM", item.stock_uom, "must_be_whole_number") and counted != int(counted):
        frappe.throw(_("Enter a whole number of {0}").format(item.stock_uom))
    try: expected = float(expected_qty)
    except (TypeError,ValueError): expected = float('nan')
    if not math.isfinite(expected): frappe.throw(_('Expected quantity must be a finite number'))
    frappe.db.sql("select name from tabItem where name=%s for update", item_code)
    frappe.db.sql("select name from tabBin where item_code=%s and warehouse=%s for update", (item_code, warehouse))
    balance = frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, ["actual_qty", "valuation_rate"], as_dict=True) or frappe._dict(actual_qty=0, valuation_rate=0)
    before = flt(balance.actual_qty)
    if abs(before - expected) > 0.000001:
        frappe.throw(_("Stock changed while you counted. Refresh and check the latest quantity."))
    if abs(counted - before) < 0.000001:
        frappe.throw(_("The count matches. No correction is needed."))
    rate = flt(balance.valuation_rate)
    if counted > 0 and rate <= 0:
        rate = _number(valuation_rate, "Cost per stock unit")
    company = frappe.db.get_value("Warehouse", warehouse, "company")
    details = {"item": item_code, "name": item.item_name, "uom": item.stock_uom,
               "before": before, "counted": counted, "difference": counted - before, "comment": comment}
    doc = frappe.get_doc({"doctype": "Stock Reconciliation", "purpose": "Stock Reconciliation",
        "company": company, "posting_date": nowdate(), "posting_time": nowtime(), "set_posting_time": 1,
        "expense_account": frappe.db.get_value("Company", company, "stock_adjustment_account"),
        "cost_center": branch.cost_center(restaurant), "luuv_audit_restaurant": restaurant,
        "luuv_audit_details": frappe.as_json(details),
        "items": [{"item_code": item_code, "warehouse": warehouse, "qty": counted, "valuation_rate": rate}]})
    if before < 0:
        from luuvcrm.branch_stock_entry import BranchStockReconciliation
        doc = BranchStockReconciliation(doc.as_dict())
        doc.flags.luuv_correcting_negative_stock = True
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    doc.flags.ignore_permissions = True
    doc.submit()
    doc.add_comment("Comment", comment)
    activity.log_activity("stock.audit", label="Stock count corrected: " + item.item_name,
        ref_doctype="Stock Reconciliation", ref_name=doc.name, item=item_code, qty=counted - before,
        detail=details)
    return {"name": doc.name, "details": details}
