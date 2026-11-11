"""Real ERPNext order/stock/payment regression checks; every fixture rolls back."""
import json
import math
import sys
from unittest.mock import patch

sys.path.insert(0, '/tmp/luuv-pos-inventory-20261111/stage')
import frappe
import luuvcrm
assert '/tmp/luuv-pos-inventory-20261111/stage/' in luuvcrm.__file__
frappe.init(site='fryxo')
frappe.connect()
frappe.set_user('Administrator')
token = 'LFIP-' + frappe.generate_hash(length=8)
try:
    from luuvcrm import branch, activity
    from luuvcrm.api import orders, payments, inventory, pos_inventory as pos, manager_inventory as manager
    from erpnext.stock.utils import get_stock_balance
    # The running web app's cached hooks still belong to the previous release.
    # Exercise the staged hook through native Document.save without changing Redis.
    from luuvcrm.hooks import doc_events
    hooks = frappe.get_doc_hooks()
    hooks.setdefault('POS Order', {})['validate'] = [doc_events['POS Order']['validate']]
    warehouse = frappe.db.get_value('Restaurant', 'Kurunegala', 'warehouse')

    def call(fn, **data):
        old = frappe.local.form_dict
        frappe.local.form_dict = frappe._dict(data)
        try:
            return fn()
        finally:
            frappe.local.form_dict = old

    def blocked(fn, text):
        point = 'check_' + frappe.generate_hash(length=8)
        frappe.db.savepoint(point)
        try:
            fn()
        except frappe.ValidationError as exc:
            assert text.lower() in str(exc).lower(), str(exc)
            frappe.db.rollback(save_point=point)
            frappe.clear_messages()
        else:
            raise AssertionError('Expected rejection: ' + text)

    with patch.object(frappe.db, 'commit'), patch.object(frappe, 'enqueue'), \
         patch.object(frappe, 'publish_realtime'), patch.object(activity, 'log_activity'), \
         patch.object(branch, 'selected', return_value='Kurunegala'), \
         patch.object(payments, '_active_pos_profile_name', return_value='Kurunegala'), \
         patch.object(payments, '_ensure_open_shift'):
        if not frappe.db.exists('POS Opening Entry', {'user': 'Administrator', 'pos_profile': 'Kurunegala', 'status': 'Open', 'docstatus': 1}):
            opening = frappe.get_doc(dict(doctype='POS Opening Entry', user='Administrator',
                pos_profile='Kurunegala', company='Luuv Fryxo',
                period_start_date=frappe.utils.now_datetime(), posting_date=frappe.utils.today(),
                balance_details=[dict(mode_of_payment='Cash', opening_amount=0)]))
            opening.insert(ignore_permissions=True)
            opening.submit()
        raw = manager.create_ingredient(token + '-RAW', 'Kg', restaurant='Kurunegala')['item']
        meal, unmapped = token + '-MEAL', token + '-UNMAPPED'
        for code in (meal, unmapped):
            frappe.get_doc(dict(doctype='Item', item_code=code, item_name=code,
                               item_group='Raw Materials', stock_uom='Nos', is_stock_item=0,
                               is_sales_item=1, standard_rate=100)).insert(ignore_permissions=True)
        manager.correct_stock(raw, .15, 0, 'Rollback-only opening stock',
                              valuation_rate=100, restaurant='Kurunegala')
        with patch.object(manager, '_menu', return_value=[meal, unmapped]):
            draft_recipe = manager.save_recipe(meal, [dict(item=raw, qty=150)], restaurant='Kurunegala')
            manager.activate_recipe(meal, draft_recipe['version'], 'Kurunegala')
        manager.correct_stock(raw, 0, .15, 'Rollback-only empty stock', restaurant='Kurunegala')
        def balance():
            return float(get_stock_balance(raw, warehouse) or 0)
        def payload(item=meal, qty=1, **extra):
            return dict(items=json.dumps([dict(item=item, qty=qty)]), customer_name=token,
                        order_source='Walk-in', order_type='Take Away', **extra)
        def new(fn=orders.send_to_kitchen, item=meal, qty=1, **extra):
            return call(fn, **payload(item, qty, **extra))['name']
        assert balance() == 0

        ordinary = new(qty=2)
        plain_unmapped = new(item=unmapped)
        assert not frappe.db.get_value('POS Order', ordinary, pos.FIELD)
        call(orders.mark_kot_printed, order_name=ordinary)
        assert inventory.consume_order_stock(ordinary)['skipped'] == 'ordinary_pos'
        call(payments.process_payment, order_name=ordinary, payment_mode='Cash')
        paid_plain = new(payments.place_order, item=unmapped, payment_mode='Cash')
        assert frappe.db.get_value('POS Order', ordinary, 'pos_invoice')
        assert frappe.db.get_value('POS Order', paid_plain, 'pos_invoice')
        assert balance() == 0
        assert not frappe.db.exists('Stock Entry', {'luuv_pos_order': ordinary})
        print('PASS ordinary POS: zero stock + missing recipe, KOT and real payment, no deductions', flush=True)

        blocked(lambda: new(pos.send_to_kitchen, item=unmapped), 'recipe is not configured')
        blocked(lambda: new(pos.send_to_kitchen), 'Insufficient stock')
        blocked(lambda: new(pos.place_order, item=unmapped, payment_mode='Cash'), 'recipe is not configured')
        assert not frappe.flags.get('inventory_pos_create')
        tracked = new(pos.send_to_kitchen, draft=1)
        assert frappe.db.get_value('POS Order', tracked, pos.FIELD) == 1
        blocked(lambda: pos.prepare_kot(tracked), 'Insufficient stock')
        blocked(lambda: call(orders.mark_kot_printed, order_name=tracked), 'Insufficient stock')
        blocked(lambda: call(payments.process_payment, order_name=tracked, payment_mode='Cash'), 'Insufficient stock')
        blocked(lambda: call(payments.pay_split, order_name=tracked,
                            lines=json.dumps([dict(item=meal, qty=1)]), payment_mode='Cash'), 'Insufficient stock')
        assert frappe.db.get_value('POS Order', tracked, 'kitchen_status') == 'Draft'
        assert not frappe.db.get_value('POS Order', tracked, 'pos_invoice')
        doc = frappe.get_doc('POS Order', tracked)
        doc.set(pos.FIELD, 0)
        blocked(lambda: doc.save(ignore_permissions=True), 'inventory mode cannot be changed')
        print('PASS inventory POS: missing recipes, zero stock, KOT, full/split payment and mode tampering blocked', flush=True)

        table = frappe.db.get_value('Restaurant Table', {'restaurant': 'Kurunegala'}, 'name')
        assert table
        ordinary_draft = new(draft=1, table=table)
        inventory_draft = new(pos.send_to_kitchen, draft=1, table=table)
        assert ordinary_draft != inventory_draft
        assert new(pos.send_to_kitchen, draft=1, table=table) == inventory_draft
        for channel, wanted, hidden in [('legacy', ordinary_draft, inventory_draft),
                                        ('inventory', inventory_draft, ordinary_draft)]:
            for fn in (orders.get_ongoing_orders, orders.get_table_orders):
                names = {row['name'] for row in call(fn, pos_channel=channel, table=table)['orders']}
                assert wanted in names and hidden not in names, (channel, names)
        assert {ordinary_draft, inventory_draft} <= {
            row['name'] for row in call(orders.get_table_orders, table=table)['orders']}
        amended = new(draft=1, amended_from=inventory_draft)
        assert frappe.db.get_value('POS Order', amended, pos.FIELD) == 1
        print('PASS separate POS lists and duplicate guards; shared waiter view and amended mode preserved', flush=True)

        manager.correct_stock(raw, .45, 0, 'Rollback-only POS release check',
                              valuation_rate=100, restaurant='Kurunegala')
        pos.prepare_kot(tracked)
        assert math.isclose(balance(), .30, abs_tol=1e-6)
        assert pos.prepare_kot(tracked)['stock']['consumed'] == 0
        call(orders.mark_kot_printed, order_name=tracked)
        call(payments.process_payment, order_name=tracked, payment_mode='Cash')
        assert math.isclose(balance(), .30, abs_tol=1e-6)
        paid_inventory = new(pos.place_order, payment_mode='Cash')
        assert frappe.db.get_value('POS Order', paid_inventory, pos.FIELD) == 1
        assert math.isclose(balance(), .15, abs_tol=1e-6)
        call(orders.update_order, order_name=inventory_draft, **payload(qty=1))
        pos.prepare_kot(inventory_draft)
        assert math.isclose(balance(), 0, abs_tol=1e-6)
        assert inventory.consume_order_stock(inventory_draft)['consumed'] == 0
        assert frappe.db.count('Stock Entry', {'luuv_pos_order': inventory_draft, 'docstatus': 1}) == 1
        print('PASS real stock issue, draft edits, repeated KOT and payments deduct exactly once', flush=True)

    frappe.db.rollback()
    assert not frappe.db.exists('Item', raw)
    assert not frappe.db.exists('Item', meal)
    assert not frappe.db.exists('POS Order', {'customer_name': token})
    assert not frappe.db.exists('Stock Ledger Entry', {'item_code': raw})
    print('PASS all test orders, invoices, recipes and stock rolled back', flush=True)
finally:
    frappe.db.rollback()
    frappe.destroy()
