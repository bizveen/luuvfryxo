"""Approved bill corrections: new stock movements and an immutable Version trail."""
from collections import defaultdict
import hashlib
import json
import math
import re

import frappe
from frappe import _
from frappe.utils import flt, now_datetime
from luuvcrm import activity, branch, roles
from luuvcrm.api import inventory
from luuvcrm.api.expenses import category_is_stock, category_is_fixed_asset, FOOD_PURCHASE_CATEGORY
from luuvcrm.api.purchases import _bill_dict, _purchase_line_category

KEY = 'purchase_category_correction'


def _fingerprint(doc):
    return hashlib.sha256(json.dumps([[r.name, r.item, flt(r.qty), r.uom, flt(r.price)]
        for r in doc.items], sort_keys=True).encode()).hexdigest()


def _bill(name):
    roles.require_manager()
    branch.require_access('Purchase Bill', name)
    doc = frappe.get_doc('Purchase Bill', name, for_update=True)
    if doc.status != 'approved':
        frappe.throw(_('Only approved purchase bills can use this category correction.'))
    return doc


def _history(name, request_id=None):
    pattern = '%' + (request_id or KEY) + '%'
    rows = frappe.db.sql('''SELECT name, owner, creation, data FROM tabVersion
        WHERE ref_doctype='Purchase Bill' AND docname=%s AND data LIKE %s
        ORDER BY creation DESC, name DESC LIMIT 30 FOR UPDATE''', (name, pattern), as_dict=True)
    result = []
    for row in rows:
        record = frappe.parse_json(row.data or '{}').get(KEY)
        if record and (not request_id or record.get('request_id') == request_id):
            result.append(dict(record, version=row.name, by=row.owner, when=str(row.creation)))
    return result


def _row_info(doc):
    result = {}
    for row in doc.items:
        item = frappe.db.get_value('Item', row.item,
            ['is_stock_item', 'stock_uom', 'is_fixed_asset', 'has_batch_no', 'has_serial_no'], as_dict=True) if row.item else None
        factor = 1.0
        if item and row.uom and row.uom != item.stock_uom:
            from erpnext.stock.get_item_details import get_conversion_factor
            factor = flt(get_conversion_factor(row.item, row.uom).get('conversion_factor'))
        is_asset = bool(row.get('asset') or (item and item.is_fixed_asset) or category_is_fixed_asset(row.get('category')))
        result[row.name] = dict(item=row.item, qty=flt(row.qty)*factor, factor=factor,
            unit=item.stock_uom if item else row.uom, rate=flt(row.price)/factor if factor else 0,
            can_receive=bool(item and item.is_stock_item and factor > 0 and flt(row.qty) > 0 and not is_asset),
            can_change=not is_asset and not bool(item and (item.has_batch_no or item.has_serial_no)),
            tracked=bool(item and (item.has_batch_no or item.has_serial_no)))
    return result


def _state(doc, info, history):
    restaurant, warehouse, company = inventory._stock_context(doc)
    if history:
        previous = history[0]
        if previous['line_fingerprint'] != _fingerprint(doc):
            frappe.throw(_('Bill items changed after a stock correction. Review its history before continuing.'))
        if previous['warehouse'] != warehouse:
            frappe.throw(_('This bill was corrected in a different warehouse. Review its stock history first.'))
        for entry in previous['stock_entries']:
            if frappe.db.get_value('Stock Entry', entry, 'docstatus') != 1:
                frappe.throw(_('A stock entry for this bill was cancelled. Review its stock history first.'))
        return dict(previous['allocations']), list(previous['stock_entries']), (restaurant, warehouse, company)
    allocations = {row.name: 0.0 for row in doc.items}
    entry_name = doc.get('stock_entry') or frappe.db.get_value('Stock Entry',
        {'luuv_purchase_bill': doc.name, 'docstatus': 1, 'purpose': 'Material Receipt'}, 'name')
    if not entry_name or frappe.db.get_value('Stock Entry', entry_name, 'docstatus') != 1:
        return allocations, [], (restaurant, warehouse, company)
    entry = frappe.get_doc('Stock Entry', entry_name)
    actual, expected = defaultdict(float), defaultdict(float)
    if entry.purpose != 'Material Receipt' or entry.company != company:
        frappe.throw(_('Review the original stock receipt before correcting this bill.'))
    for line in entry.items:
        if line.t_warehouse != warehouse or line.s_warehouse:
            frappe.throw(_('This bill was received into a different warehouse. Review the original receipt first.'))
        actual[line.item_code] += flt(line.transfer_qty)
    for row in doc.items:
        if category_is_stock(row.get('category')) and info[row.name]['can_receive']:
            allocations[row.name] = info[row.name]['qty']
            expected[row.item] += allocations[row.name]
    if set(actual) != set(expected) or any(not math.isclose(actual[k], expected[k], abs_tol=1e-6) for k in actual):
        frappe.throw(_('The original stock receipt does not match this bill. Review it before changing categories.'))
    return allocations, [entry_name], (restaurant, warehouse, company)


def _review(doc):
    history = _history(doc.name)
    info = _row_info(doc)
    allocations, entries, context = _state(doc, info, history)
    bill = _bill_dict(doc.name)
    for line in bill['lines']:
        row = info[line['row_name']]
        line.update(stock_qty=row['qty'], stock_unit=row['unit'], received_qty=allocations.get(line['row_name'], 0),
                    can_receive=row['can_receive'], can_change=row['can_change'])
    options = [dict(value=row.name, label=row.name, in_stock=bool(category_is_stock(row.name)))
        for row in frappe.get_all('Expense Category', filters={'disabled': 0}, fields=['name'], order_by='name')
        if not category_is_fixed_asset(row.name)]
    return dict(bill=bill, categories=options, warehouse=context[1], history=history,
                balances={r.item: flt(frappe.db.get_value('Bin', {'item_code': r.item, 'warehouse': context[1]}, 'actual_qty'))
                          for r in doc.items if r.item and info[r.name]['can_receive']},
                revision=str(doc.modified)+'|'+(history[0]['version'] if history else ''))


@frappe.whitelist()
def review(name):
    return _review(_bill(name))


@frappe.whitelist(methods=['POST'])
def correct(name, line_categories, expected_revision, request_id, comment=''):
    doc = _bill(name)
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', str(request_id or '')):
        frappe.throw(_('Reload this correction and try again.'))
    changes = frappe.parse_json(line_categories) if isinstance(line_categories, str) else line_categories
    if not isinstance(changes, list) or len(changes) > len(doc.items):
        frappe.throw(_('Choose a category for each bill item.'))
    comment = str(comment or '').strip()
    if len(comment) > 500: frappe.throw(_('Keep the correction note within 500 characters.'))
    digest = hashlib.sha256(json.dumps([changes, comment], sort_keys=True).encode()).hexdigest()
    retry = _history(name, request_id)
    if retry:
        if retry[0]['payload_hash'] != digest: frappe.throw(_('This correction request was already used. Reload the bill.'))
        return dict(ok=True, already_processed=True, **_review(doc))
    history = _history(name)
    revision = str(doc.modified)+'|'+(history[0]['version'] if history else '')
    if expected_revision != revision:
        frappe.throw(_('This bill changed on another screen. Close and reopen the correction before saving.'))
    info = _row_info(doc)
    allocations, entries, context = _state(doc, info, history)
    restaurant, warehouse, company = context
    rows = {row.name: row for row in doc.items}
    seen, corrected = set(), []
    deltas, incoming_cost, incoming_qty, outgoing_qty = (defaultdict(float) for _ in range(4))
    for change in changes:
        key = change.get('row_name') if isinstance(change, dict) else None
        if key not in rows or key in seen: frappe.throw(_('A bill item is missing, duplicated or belongs to another bill.'))
        seen.add(key)
        row, meta = rows[key], info[key]
        if not isinstance(change.get('category'), str) or not change['category'].strip():
            frappe.throw(_('Choose an active category for every changed item.'))
        category = _purchase_line_category(change.get('category'))
        old = row.get('category') or FOOD_PURCHASE_CATEGORY
        if category == old: continue
        if not meta['can_change'] or category_is_fixed_asset(category):
            frappe.throw(_('Use the asset workflow to correct fixed-asset purchases.'))
        before = flt(allocations.get(key))
        stock = category_is_stock(category)
        if stock and not meta['can_receive']:
            frappe.throw(_('{0} needs a linked stock item and a valid unit before it can receive stock.').format(row.item_name))
        # Never invent a receipt for old stock-labelled bills that never received one.
        after = (before or (0 if category_is_stock(old) else meta['qty'])) if stock else 0
        delta = after-before
        if delta and meta['tracked']:
            frappe.throw(_('Correct batch or serial tracked items through the stock workflow.'))
        if delta:
            deltas[row.item] += delta
            if delta > 0:
                incoming_cost[row.item] += delta*meta['rate']
                incoming_qty[row.item] += delta
            else:
                outgoing_qty[row.item] -= delta
        allocations[key] = after
        corrected.append(dict(row_name=key, item=row.item, name=row.item_name or row.item,
                              before=old, after=category, stock_before=before, stock_after=after,
                              stock_delta=delta, unit=meta['unit']))
    if not corrected: return dict(ok=True, changed=False, **_review(doc))
    from luuvcrm.api.branch_stock_policy import balance
    movements = []
    for item, delta in sorted(deltas.items()):
        before = balance(item, warehouse)
        if outgoing_qty[item] and before-outgoing_qty[item] < -1e-7:
            frappe.throw(_('Not enough stock to correct {0}: available {1:g}, removal {2:g}. Check the physical stock count first.').format(item, before, outgoing_qty[item]))
        movements.append(dict(item=item, before=before, delta=delta, after=before+delta,
                              received=incoming_qty[item], issued=outgoing_qty[item]))
    new_entries = []
    # Keep the issue and receipt separate even for the same Item: purchase costs
    # may differ between rows, and netting quantities would lose that valuation.
    for purpose, incoming in [('Material Issue', False), ('Material Receipt', True)]:
        selected = [m for m in movements if m['received' if incoming else 'issued'] > 1e-7]
        if not selected: continue
        entry = inventory._new_stock_entry(purpose, company, warehouse,
            'Category correction for Purchase Bill '+name+' / '+request_id)
        for movement in selected:
            item = movement['item']
            values = dict(item_code=item, qty=movement['received' if incoming else 'issued'],
                          uom=frappe.db.get_value('Item', item, 'stock_uom'), conversion_factor=1,
                          cost_center=branch.cost_center(restaurant))
            values['t_warehouse' if incoming else 's_warehouse'] = warehouse
            if incoming:
                values.update(basic_rate=incoming_cost[item]/incoming_qty[item], set_basic_rate_manually=1)
            entry.append('items', values)
        entry = inventory._submit(entry, allow_negative_stock=incoming and any(m['before'] < 0 for m in selected))
        new_entries.append(entry.name)
    for row in corrected:
        frappe.db.set_value('Purchase Bill Item', row['row_name'], 'category', row['after'], update_modified=False)
    frappe.db.set_value('Purchase Bill', name, 'modified', now_datetime())
    record = dict(request_id=request_id, payload_hash=digest, comment=comment, changes=corrected,
                  line_fingerprint=_fingerprint(doc),
                  allocations=allocations, warehouse=warehouse, stock_entries=entries+new_entries,
                  new_stock_entries=new_entries, movements=movements)
    frappe.get_doc(dict(doctype='Version', ref_doctype='Purchase Bill', docname=name,
        data=frappe.as_json({KEY: record, 'changed': [['Category correction', '', comment or 'Item categories corrected']]}))).insert(ignore_permissions=True)
    activity.log_activity('purchase_bill.category_correction', label='Approved bill categories corrected: '+doc.vendor,
        ref_doctype='Purchase Bill', ref_name=name, amount=doc.total, detail=record)
    doc.reload()
    return dict(ok=True, changed=True, **_review(doc))
