"""Validate existing per-line category approval against native ERPNext, then roll back."""
import json
from unittest.mock import patch
import frappe
frappe.init(site='fryxo')
frappe.connect()
frappe.set_user('Administrator')
try:
    from luuvcrm import branch, activity, roles
    from luuvcrm.api import purchases, approvals, manager_inventory
    from erpnext.stock.utils import get_stock_balance
    token = 'LFCAT-' + frappe.generate_hash(length=8)
    warehouse = frappe.db.get_value('Restaurant', 'Kurunegala', 'warehouse')
    def call(fn, **data):
        old = frappe.local.form_dict
        frappe.local.form_dict = frappe._dict(data)
        try: return fn()
        finally: frappe.local.form_dict = old
    with patch.object(frappe.db, 'commit'), patch.object(frappe, 'enqueue'), \
         patch.object(frappe, 'publish_realtime'), patch.object(activity, 'log_activity'), \
         patch.object(branch, 'selected', return_value='Kurunegala'):
        raw = manager_inventory.create_ingredient(token, 'Kg', restaurant='Kurunegala')['item']
        bill = call(purchases.create_purchase_bill, vendor=token, lines=json.dumps([
            dict(item=raw, qty=2, price=100, unit='Kg', category='Food / Ingredients'),
            dict(item=raw, qty=3, price=200, unit='Kg', category='Food / Ingredients'),
        ]))['name']
        card_name = purchases._find_pending_card('purchase_bill', 'bill', bill)
        cards = call(approvals.get_pending_approvals)['approvals']
        card = next(c for c in cards if c['name'] == card_name)
        assert all(line['row_name'] and line['has_item'] for line in card['lines'])
        choices = {c['value']: c for c in card['line_category_options']}
        nonstock = next(name for name, c in choices.items()
                        if not c['in_stock'] and not c['has_item_list'] and not c['is_fixed_asset'])
        changes = [dict(row_name=card['lines'][0]['row_name'], category=nonstock)]
        # Other authorization stays live; this fixture's requester is Administrator.
        # The UI self-approval lock is separately covered by the JS regression check.
        with patch.object(roles, 'block_self_approval'):
            point = 'bad_category'
            frappe.db.savepoint(point)
            try:
                call(approvals.resolve_approval, name=card_name, decision='approved',
                     line_categories=json.dumps([dict(row_name='another-bill-row', category=nonstock)]))
            except frappe.ValidationError:
                frappe.db.rollback(save_point=point)
                frappe.clear_messages()
            else: raise AssertionError('Foreign bill row accepted')
            assert frappe.db.get_value('POS Approval', card_name, 'status') == 'pending'
            assert not get_stock_balance(raw, warehouse)
            call(approvals.resolve_approval, name=card_name, decision='approved',
                 line_categories=json.dumps(changes), comment='Rollback-only category review')
        doc = frappe.get_doc('Purchase Bill', bill)
        assert [row.category for row in doc.items] == [nonstock, 'Food / Ingredients']
        assert float(doc.total) == 800
        assert not doc.items[0].item and float(doc.items[0].qty) == 1 and float(doc.items[0].price) == 200
        assert float(get_stock_balance(raw, warehouse)) == 3
        entry = frappe.get_doc('Stock Entry', doc.stock_entry)
        assert len(entry.items) == 1 and entry.items[0].item_code == raw and float(entry.items[0].qty) == 3
        try: purchases._apply_purchase_line_categories(doc, changes)
        except frappe.ValidationError: frappe.clear_messages()
        else: raise AssertionError('Approved bill categories could be rewritten')
        print('PASS actual approvals: stable row IDs/options, invalid row rollback, individual category change, unchanged bill total and stock receipt only for stock line', flush=True)
        print('PASS approved purchase history remains locked', flush=True)
    frappe.db.rollback()
    assert not frappe.db.exists('Item', raw)
    assert not frappe.db.exists('Purchase Bill', bill)
    assert not frappe.db.exists('Stock Ledger Entry', {'item_code': raw})
    print('PASS all sample bills, prices and stock rolled back', flush=True)
finally:
    frappe.db.rollback()
    frappe.destroy()
