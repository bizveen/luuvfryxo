"""Fixed assets bought through the restaurant purchase-bill workflow.

The custom Purchase Bill is the staff-facing receipt and approval trail.  It is
not a GL document, so approval creates a *draft* ERPNext Asset for each fixed-
asset line.  The accountant can review depreciation and the opening/capital
entry before submitting it; the purchase never leaks into operating P&L while
that review is pending.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt

from luuvcrm import activity, branch
from luuvcrm.api.expenses import category_asset_category, category_is_fixed_asset


ASSET_ACCOUNT_CANDIDATES = {
    "Kitchen Equipment": ("Capital Equipments", "Plants and Machineries", "Office Equipments"),
    "Electrical Equipment": ("Electronic Equipments", "Capital Equipments", "Office Equipments"),
    "Furniture & Fixtures": ("Furnitures and Fixtures", "Office Equipments", "Capital Equipments"),
    "Signage": ("Office Equipments", "Capital Equipments", "Furnitures and Fixtures"),
}


def _account(company, account_type, preferred=()):
    rows = frappe.get_all(
        "Account",
        filters={"company": company, "account_type": account_type, "is_group": 0},
        fields=["name", "account_name"],
        order_by="name",
    )
    for wanted in preferred:
        for row in rows:
            if (row.account_name or "").casefold() == wanted.casefold():
                return row.name
    return rows[0].name if rows else None


def ensure_asset_categories(companies=None):
    """Provision the four restaurant Asset Categories when accounts permit it.

    Existing categories and account mappings are never replaced.  A company
    with a non-standard chart and no typed fixed-asset/depreciation accounts is
    left for its accountant to configure instead of guessing a ledger account.
    """
    from luuvcrm.api.expenses import FIXED_ASSET_PURCHASE_CATEGORIES

    companies = companies or frappe.get_all("Company", pluck="name")
    configured = []
    skipped = []
    for purchase_category, asset_category in FIXED_ASSET_PURCHASE_CATEGORIES.items():
        if not frappe.db.exists("Asset Category", asset_category):
            doc = frappe.get_doc({
                "doctype": "Asset Category",
                "asset_category_name": asset_category,
                "enable_cwip_accounting": 0,
                "accounts": [],
            })
        else:
            doc = frappe.get_doc("Asset Category", asset_category)
        existing = {row.company_name for row in (doc.accounts or [])}
        changed = doc.is_new()
        for company in companies:
            if company in existing:
                continue
            fixed = _account(
                company, "Fixed Asset", ASSET_ACCOUNT_CANDIDATES.get(asset_category, ()),
            )
            accumulated = _account(company, "Accumulated Depreciation")
            depreciation = _account(company, "Depreciation")
            if not (fixed and accumulated and depreciation):
                skipped.append({"asset_category": asset_category, "company": company})
                continue
            doc.append("accounts", {
                "company_name": company,
                "fixed_asset_account": fixed,
                "accumulated_depreciation_account": accumulated,
                "depreciation_expense_account": depreciation,
            })
            changed = True
        if changed and doc.accounts:
            doc.flags.ignore_permissions = True
            if doc.is_new():
                doc.insert(ignore_permissions=True)
            else:
                doc.save(ignore_permissions=True)
        if frappe.db.exists("Asset Category", asset_category):
            configured.append(asset_category)
    return {"configured": sorted(set(configured)), "skipped": skipped}


def ensure_asset_purchase_categories():
    """Create/update the staff-facing categories and their ERPNext mapping."""
    from luuvcrm.api.expenses import FIXED_ASSET_PURCHASE_CATEGORIES

    if not frappe.db.exists("DocType", "Expense Category"):
        return {"categories": [], "assets": {"configured": [], "skipped": []}}
    assets = ensure_asset_categories()
    categories = []
    has_asset_fields = (
        frappe.db.has_column("Expense Category", "is_fixed_asset")
        and frappe.db.has_column("Expense Category", "asset_category")
    )
    for name, asset_category in FIXED_ASSET_PURCHASE_CATEGORIES.items():
        # A non-standard chart of accounts may not have enough typed accounts
        # to configure this Asset Category safely.  Do not create a dangling
        # Link; the setup result tells the accountant what was skipped.
        if has_asset_fields and not frappe.db.exists("Asset Category", asset_category):
            continue
        if not frappe.db.exists("Expense Category", name):
            values = {
                "doctype": "Expense Category", "category_name": name,
                "in_stock": 0, "has_item_list": 0,
            }
            if has_asset_fields:
                values.update({"is_fixed_asset": 1, "asset_category": asset_category})
            frappe.get_doc(values).insert(ignore_permissions=True)
            categories.append(name)
        elif has_asset_fields:
            frappe.db.set_value("Expense Category", name, {
                "in_stock": 0,
                "has_item_list": 0,
                "is_fixed_asset": 1,
                "asset_category": asset_category,
            }, update_modified=False)
    return {"categories": categories, "assets": assets}


def ensure_asset_item(item_name, purchase_category, uom="Nos"):
    """Return a fixed-asset Item for a free-text purchase-bill line.

    Asset purchase categories deliberately have no item picker. The buyer types
    "Fridge" or "AC" on the receipt, and this creates/reuses the ERPNext Item
    that the draft Asset requires behind the scenes.
    """
    item_name = " ".join((item_name or "").split())
    if not item_name:
        frappe.throw(_("Every fixed-asset line needs a name"))
    asset_category = category_asset_category(purchase_category)
    if not asset_category or not frappe.db.exists("Asset Category", asset_category):
        frappe.throw(_("Configure ERPNext Asset Category {0} first").format(
            asset_category or purchase_category,
        ))
    existing = frappe.db.get_value(
        "Item",
        {"item_name": item_name, "is_fixed_asset": 1,
         "asset_category": asset_category, "disabled": 0},
        "name",
    )
    if existing:
        return existing
    unit = (uom or "Nos").strip() or "Nos"
    if not frappe.db.exists("UOM", unit):
        frappe.get_doc({"doctype": "UOM", "uom_name": unit}).insert(ignore_permissions=True)
    group = ("Products" if frappe.db.exists("Item Group", "Products") else
             frappe.db.get_value("Item Group", {"is_group": 0}, "name"))
    code = item_name
    if frappe.db.exists("Item", code):
        code = "{0} - {1}".format(item_name, asset_category)
    if frappe.db.exists("Item", code):
        frappe.throw(_("An Item named {0} already exists with different asset settings").format(
            item_name,
        ))
    doc = frappe.get_doc({
        "doctype": "Item",
        "item_code": code,
        "item_name": item_name,
        "item_group": group,
        "stock_uom": unit,
        "is_stock_item": 0,
        "is_fixed_asset": 1,
        "asset_category": asset_category,
        "auto_create_assets": 0,
        "is_grouped_asset": 1,
        "is_purchase_item": 1,
        "is_sales_item": 0,
    })
    doc.insert(ignore_permissions=True)
    return doc.name


def _asset_context(purchase_bill):
    restaurant = purchase_bill.get(branch.FIELD) or branch.current()
    if not restaurant:
        frappe.throw(_("Choose a restaurant before approving fixed-asset purchases"))
    cost_center = branch.cost_center(restaurant)
    company = frappe.db.get_value("Restaurant", restaurant, "company")
    if not company and cost_center:
        company = frappe.db.get_value("Cost Center", cost_center, "company")
    if not company:
        warehouse = branch.warehouse(restaurant)
        company = frappe.db.get_value("Warehouse", warehouse, "company") if warehouse else None
    if not company:
        company = frappe.defaults.get_global_default("company")
    if not company:
        frappe.throw(_("Set a Company on Restaurant {0}").format(restaurant))
    if not cost_center:
        frappe.throw(_("Set a Cost Center on Restaurant {0}").format(restaurant))
    location = "{0} Restaurant".format(restaurant)
    if not frappe.db.exists("Location", location):
        frappe.get_doc({
            "doctype": "Location", "location_name": location, "is_group": 0,
        }).insert(ignore_permissions=True)
    return restaurant, company, cost_center, location


def validate_asset_item(item_code, purchase_category):
    """Refuse a normal stock/expense Item on a fixed-asset line."""
    asset_category = category_asset_category(purchase_category)
    if not asset_category or not frappe.db.exists("Asset Category", asset_category):
        frappe.throw(_(
            "Configure ERPNext Asset Category {0} before using purchase category {1}"
        ).format(asset_category or purchase_category, purchase_category))
    item = frappe.db.get_value(
        "Item", item_code, ["is_fixed_asset", "is_stock_item", "asset_category"], as_dict=True,
    )
    if not item or not item.is_fixed_asset or item.is_stock_item:
        frappe.throw(_("{0} must be a non-stock Fixed Asset Item").format(item_code))
    if item.asset_category != asset_category:
        frappe.throw(_("{0} belongs to Asset Category {1}, not {2}").format(
            item_code, item.asset_category or _("none"), asset_category,
        ))
    return asset_category


def create_purchase_assets(purchase_bill):
    """Create one grouped draft Asset per fixed-asset bill line, exactly once."""
    doc = (frappe.get_doc("Purchase Bill", purchase_bill)
           if isinstance(purchase_bill, str) else purchase_bill)
    asset_lines = [line for line in (doc.get("items") or [])
                   if category_is_fixed_asset(line.get("category"))]
    if not asset_lines:
        return []
    if not frappe.db.has_column("Purchase Bill Item", "asset"):
        frappe.throw(_("Run bench migrate before approving fixed-asset purchases"))

    restaurant, company, cost_center, location = _asset_context(doc)
    subtotal = sum(flt(line.qty) * flt(line.price) for line in (doc.get("items") or []))
    ratio = flt(doc.total) / subtotal if subtotal else 1
    created = []
    for line in asset_lines:
        if line.get("asset") and frappe.db.exists("Asset", line.asset):
            created.append(line.asset)
            continue
        qty = flt(line.qty)
        if qty <= 0 or qty != cint(qty):
            frappe.throw(_("Fixed asset quantity for {0} must be a whole number").format(
                line.item_name or line.item,
            ))
        asset_category = validate_asset_item(line.item, line.category)
        amount = flt(qty * flt(line.price) * ratio, 2)
        if amount <= 0:
            frappe.throw(_("Fixed asset {0} needs a purchase value").format(
                line.item_name or line.item,
            ))
        asset = frappe.get_doc({
            "doctype": "Asset",
            "naming_series": "ACC-ASS-.YYYY.-",
            "asset_name": "{0} - {1}".format(line.item_name or line.item, restaurant),
            "item_code": line.item,
            "company": company,
            "location": location,
            "cost_center": cost_center,
            "asset_owner": "Company",
            "asset_owner_company": company,
            # The LuuvCRM bill is not a GL Purchase Invoice. Existing Asset keeps
            # the generated record draft/reviewable without inventing a ledger entry.
            "is_existing_asset": 1,
            "purchase_date": doc.purchase_date,
            "available_for_use_date": doc.purchase_date,
            "gross_purchase_amount": amount,
            "asset_quantity": cint(qty),
            "calculate_depreciation": 0,
        })
        asset.flags.ignore_permissions = True
        asset.insert(ignore_permissions=True)
        frappe.db.set_value("Purchase Bill Item", line.name, "asset", asset.name,
                            update_modified=False)
        created.append(asset.name)
        activity.log_activity(
            "asset.create",
            label="Draft asset created: %s" % (line.item_name or line.item),
            ref_doctype="Asset", ref_name=asset.name, item=line.item,
            amount=amount, qty=qty,
            detail={"purchase_bill": doc.name, "purchase_bill_item": line.name,
                    "restaurant": restaurant, "asset_category": asset_category},
        )
    return created
