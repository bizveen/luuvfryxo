"""Correct an unused Item's base unit without reinterpreting stock or history."""
import frappe
from frappe import _
from frappe.utils import flt
from luuvcrm import activity, roles


def defined_units():
    return frappe.get_all('UOM', filters={'enabled': 1},
        fields=['name', 'must_be_whole_number'], order_by='name', limit_page_length=0)


def validate_unit(unit):
    if not isinstance(unit, str) or not frappe.db.exists('UOM', {'name': unit, 'enabled': 1}):
        frappe.throw(_('Choose an enabled, defined unit'))
    return unit


def usage_reasons(doc):
    """Check every branch: Item units are shared, even when the editor is scoped."""
    reasons = []
    references = [
        ('Stock Ledger Entry', 'item_code', 'Stock movement history'),
        ('Stock Entry Detail', 'item_code', 'Stock entries'),
        ('Stock Reconciliation Item', 'item_code', 'Stock audits'),
        ('Purchase Bill Item', 'item', 'Purchase bills'),
        ('POS Order Item', 'item', 'POS orders'),
        ('BOM Item', 'item_code', 'Recipe ingredients'),
        ('BOM', 'item', 'Prepared-item or menu recipes'),
        ('Purchase Order Item', 'item_code', 'Purchase orders'),
        ('Purchase Receipt Item', 'item_code', 'Purchase receipts'),
        ('Purchase Invoice Item', 'item_code', 'Supplier invoices'),
        ('Sales Order Item', 'item_code', 'Sales orders'),
        ('Delivery Note Item', 'item_code', 'Delivery notes'),
        ('Sales Invoice Item', 'item_code', 'Sales invoices'),
        ('POS Invoice Item', 'item_code', 'POS invoices'),
        ('Material Request Item', 'item_code', 'Material requests'),
        ('Asset', 'item_code', 'Registered assets'),
        ('Item Price', 'item_code', 'Price book entries'),
    ]
    for doctype, field, label in references:
        if frappe.db.exists('DocType', doctype) and frappe.db.has_column(doctype, field):
            if frappe.db.exists(doctype, {field: doc.name}):
                reasons.append(label)
    if frappe.db.exists('Bin', {'item_code': doc.name, 'actual_qty': ['!=', 0]}):
        reasons.append('Existing stock balance')
    if any(flt(doc.get(field)) for field in ('standard_rate', 'valuation_rate', 'last_purchase_rate')):
        reasons.append('Saved item prices or valuation')
    if doc.has_variants or doc.variant_of:
        reasons.append('Item variants')
    if any(row.uom != doc.stock_uom for row in doc.get('uoms') or []):
        reasons.append('Additional unit conversions')
    if any(doc.get(field) and doc.get(field) != doc.stock_uom for field in ('purchase_uom', 'sales_uom')):
        reasons.append('Separate purchase or sales units')
    if frappe.db.has_column('Warehouse', 'luuv_stock_minimums'):
        for raw in frappe.get_all('Warehouse', filters={'luuv_stock_minimums': ['is', 'set']}, pluck='luuv_stock_minimums'):
            if flt((frappe.parse_json(raw) or {}).get(doc.name)):
                reasons.append('Minimum stock levels');break
    return reasons


def describe(item):
    doc = frappe.get_doc('Item', item)
    reasons = usage_reasons(doc)
    history = []
    for version in frappe.get_all('Version', filters={'ref_doctype': 'Item', 'docname': item,
            'data': ['like', '%inventory_unit_change%']}, fields=['data', 'owner', 'creation'],
            order_by='creation desc', limit_page_length=5):
        record = (frappe.parse_json(version.data) or {}).get('inventory_unit_change')
        if record:
            history.append(dict(before=record['before'], after=record['after'], when=str(version.creation),
                by=frappe.get_value('User', version.owner, 'full_name') or version.owner))
    return dict(item=item, unit=doc.stock_uom, units=defined_units(), can_change=not reasons,
        reasons=reasons, history=history)


def change(doc, unit, expected_unit):
    """Caller holds the Item row lock and has checked screen/branch access."""
    before = doc.stock_uom
    if unit == before:
        return False
    if expected_unit != before:
        frappe.throw(_('Reopen this item in Manager 2.0 or Purchase 2.0 before changing its unit'))
    validate_unit(unit)
    reasons = usage_reasons(doc)
    if reasons:
        frappe.throw(_('The stock unit is locked because this item is already used in: {0}. '
                       'Its existing quantities and history must keep their original unit.').format(', '.join(reasons)))
    doc.stock_uom = unit
    # There are no additional conversions or recorded uses at this point.
    doc.set('uoms', [dict(uom=unit, conversion_factor=1)])
    for field in ('purchase_uom', 'sales_uom'):
        if doc.get(field) == before:
            doc.set(field, unit)
    doc.save(ignore_permissions=True)  # Keep ERPNext's own unit validation.
    record = dict(before=before, after=unit)
    frappe.get_doc(dict(doctype='Version', ref_doctype='Item', docname=doc.name,
        data=frappe.as_json({'inventory_unit_change': record}))).insert(ignore_permissions=True)
    activity.log_activity('item.unit', label='Item unit changed: '+doc.item_name,
        ref_doctype='Item', ref_name=doc.name, item=doc.name, item_name=doc.item_name, detail=record)
    frappe.clear_document_cache('Item', doc.name)
    return True


@frappe.whitelist()
def purchase_item(item):
    roles.require_purchases()
    if not frappe.db.exists('Item', {'name': item, 'disabled': 0, 'is_purchase_item': 1}):
        frappe.throw(_('Choose an enabled purchase item'))
    return describe(item)
