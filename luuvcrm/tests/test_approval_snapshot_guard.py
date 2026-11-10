"""Exercise the actual approvals functions with an in-memory Frappe boundary.

No network, Frappe install, or database access. Run from any directory:
python path/to/luuvcrm/tests/test_approval_snapshot_guard.py
"""
import ast
import copy
import datetime
import hashlib
import json
import pathlib
import types
import unittest

SOURCE = pathlib.Path(__file__).resolve().parents[1] / 'api' / 'approvals.py'


class D(dict):
    def __getattr__(self, name):
        if name == 'items' and 'items' in self:
            return self['items']
        return self.get(name)
    def __setattr__(self, name, value):
        self[name] = value
    def __getattribute__(self, name):
        if name == 'items' and dict.__contains__(self, 'items'):
            return dict.__getitem__(self, 'items')
        return dict.__getattribute__(self, name)


class Doc(D):
    def insert(self, **kwargs):
        self.name = 'approval-%d' % (len(self._db.approvals) + 1)
        self._db.approvals[self.name] = self
        return self
    def append(self, key, values):
        row = D(copy.deepcopy(values))
        row.setdefault('name', 'new-row-%d' % len(self[key]))
        for field in ('sent_qty', 'paid_qty', 'stock_consumed_qty'):
            row.setdefault(field, 0)
        self[key].append(row)
        return row
    def save(self, **kwargs):
        self.saved = True
        return self
    def db_set(self, field, value, **kwargs):
        self[field] = value
        return self


class DB:
    def __init__(self, order):
        self.order = order
        self.approvals = {}
        self.locks = []
    def match(self, row, filters):
        for key, value in filters.items():
            actual = row.get(key)
            if isinstance(value, (tuple, list)):
                op, wanted = value
                if op == '!=' and actual == wanted: return False
                if op == '>' and not actual > wanted: return False
                if op == '<=' and not actual <= wanted: return False
            elif actual != value:
                return False
        return True
    def rows(self, doctype):
        if doctype == 'POS Order': return [self.order]
        if doctype == 'POS Order Item': return self.order.items
        if doctype == 'POS Approval': return list(self.approvals.values())
        return []
    def exists(self, doctype, filters):
        if doctype == 'DocType': return True
        if isinstance(filters, str): filters = {'name': filters}
        return next((r.get('name') for r in self.rows(doctype) if self.match(r, filters)), None)
    def get_value(self, doctype, filters, fields='name', **kwargs):
        if doctype == 'Item': return filters
        if isinstance(filters, str): filters = {'name': filters}
        row = next((r for r in self.rows(doctype) if self.match(r, filters)), None)
        if row is None: return None
        if isinstance(fields, (list, tuple)):
            values = [row.get(f) for f in fields]
            return D(zip(fields, values)) if kwargs.get('as_dict') else values
        return row.get(fields)
    def get_all(self, doctype, filters=None, fields=None, pluck=None, **kwargs):
        rows = [r for r in self.rows(doctype) if self.match(r, filters or {})]
        if pluck: return [r.get(pluck) for r in rows]
        if fields: return [D({f: r.get(f) for f in fields}) for r in rows]
        return rows
    def set_value(self, doctype, name, field, value=None, **kwargs):
        row = next(r for r in self.rows(doctype) if r.name == name)
        row.update(field if isinstance(field, dict) else {field: value})
    def sql(self, query, values=None, **kwargs):
        self.locks.append((query, values))
        if kwargs.get('as_dict'): return [self.order]
        return [(self.order.name,)]
    def has_column(self, *args): return True


def line(code, qty=1, rate=100, **kwargs):
    return D(item=code, item_name=code, name='row-'+code, qty=qty, rate=rate,
             takeaway=0, notes='', sent_qty=0, paid_qty=0, stock_consumed_qty=0,
             **kwargs)


class ActualFunctions(unittest.TestCase):
    def setUp(self):
        self.clock = datetime.datetime(2026, 11, 10, 16, 54, 17)
        self.order = Doc(name='POS-TEST', items=[line('SHAWARMA', rate=1690), line('FRIES', rate=1690)],
                         docstatus=0, pos_invoice=None, order_type='Dine In', restaurant='Luuv Fryxo',
                         restaurant_table='Luuv-Fryxo-03', service_charge_rate=10,
                         discount_amount=0, modified=self.clock-datetime.timedelta(seconds=1), flags=D())
        self.db = DB(self.order)
        def get_doc(doctype, name=None, **kwargs):
            if kwargs.get('for_update'): self.db.locks.append((doctype, name))
            if isinstance(doctype, dict): return Doc(doctype, flags=D(), _db=self.db)
            if doctype == 'POS Order': return self.order
            if doctype == 'POS Approval': return self.db.approvals[name]
            raise AssertionError((doctype, name))
        self.frappe = types.SimpleNamespace(
            db=self.db, get_doc=get_doc, get_all=self.db.get_all,
            local=D(form_dict=D()), session=D(user='cashier'), _dict=D,
            throw=lambda message, *a: (_ for _ in ()).throw(ValueError(message)),
            parse_json=lambda v: json.loads(v) if isinstance(v, str) else v,
            as_json=lambda value: json.dumps(value, default=str),
            utils=types.SimpleNamespace(get_datetime=lambda v: datetime.datetime.fromisoformat(str(v))),
        )
        self.ns = dict(frappe=self.frappe, json=json, hashlib=hashlib, _=lambda text: text,
                       now_datetime=lambda: self.clock, get_datetime=self.frappe.utils.get_datetime,
                       roles=types.SimpleNamespace(require_pos=lambda: None,
                                                   require_purchases=lambda: None,
                                                   has_any=lambda role: True,
                                                   POS_ROLES={'cashier'}, PURCHASE_ROLES={'purchaser'},
                                                   block_self_approval=lambda *a: None),
                       branch=types.SimpleNamespace(require_access=lambda *a: None),
                       activity=types.SimpleNamespace(duplicate_approval_flag=lambda *a: (None, None),
                                                      log_activity=lambda *a, **k: 'logged'),
                       _ensure_item_notes_field=lambda: None, _clean_item_note=lambda note: str(note or '').strip(),
                       _server_item_rate=lambda code: {'SHAWARMA': 1690, 'FRIES': 1690}.get(code, 100),
                       _item_is_takeaway=lambda row, order_type: bool(row.get('takeaway')),
                       _service_charge_on=lambda base: (10, round(base / 10, 2)),
                       _apply_takeaway_service_charge=lambda doc: None)
        tree = ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
        nodes = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                node.decorator_list = []
                nodes.append(node)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                nodes.append(node)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), self.ns)
        self.ns['_order_is_moot'] = lambda name, **kwargs: None
    def request(self, items=None, extras=None):
        payload = {'items': items if items is not None else [{'item': 'FRIES', 'qty': 1, 'rate': 1690}]}
        payload.update(extras or {})
        self.frappe.local.form_dict = D(approval_type='edit', pos_order=self.order.name,
                                       action='update_items', action_payload=json.dumps(payload),
                                       title='Remove Shawarma')
        result = self.ns['create_approval']()
        return self.db.approvals[result['name']]
    def run_action(self, card):
        return self.ns['_run_bill_action'](card)
    def test_unchanged_bill_removes_only_requested_item(self):
        card = self.request()
        executed, note = self.run_action(card)
        self.assertEqual(executed, 'items_updated')
        self.assertIsNone(note)
        self.assertEqual([i.item for i in self.order.items], ['FRIES'])
    def test_05043_later_three_additions_are_not_overwritten(self):
        card = self.request()
        self.order.items.extend([line('KOTTU', rate=1790), line('COFFEE', rate=990), line('TEA', rate=580)])
        self.order.modified = self.clock + datetime.timedelta(minutes=15)
        before = copy.deepcopy(self.order.items)
        executed, note = self.run_action(card)
        self.assertIsNone(executed)
        self.assertTrue(note)
        self.assertEqual(self.order.items, before)
        self.assertFalse(self.order.get('saved'))
    def test_empty_snapshot_cannot_void_later_additions(self):
        card = self.request(items=[])
        self.order.items.append(line('KOTTU'))
        executed, note = self.run_action(card)
        self.assertIsNone(executed)
        self.assertTrue(note)
        self.assertEqual(self.order.docstatus, 0)
    def test_identical_request_dedupes(self):
        first = self.request()
        second = self.request()
        self.assertEqual(first.name, second.name)
        self.assertEqual(len(self.db.approvals), 1)
    def test_changed_quantity_rejects(self):
        card = self.request()
        self.order.items[1].qty = 2
        executed, note = self.run_action(card)
        self.assertIsNone(executed)
        self.assertTrue(note)
        self.assertEqual(self.order.items[1].qty, 2)
    def test_print_progress_does_not_make_approval_stale(self):
        card = self.request()
        self.order.items[1].sent_qty = 1
        self.order.items[1].stock_consumed_qty = 1
        self.order.modified = self.clock + datetime.timedelta(seconds=30)
        executed, note = self.run_action(card)
        self.assertEqual(executed, 'items_updated')
        self.assertIsNone(note)
        self.assertEqual(self.order.items[0].sent_qty, 1)
        self.assertEqual(self.order.items[0].stock_consumed_qty, 1)
    def test_untrusted_request_cannot_reset_server_baseline(self):
        card = self.request(extras={'_approval_base': {'items': []}})
        self.assertNotEqual(json.loads(card.action_payload).get('_approval_base'), {'items': []})
        self.order.items.append(line('KOTTU'))
        executed, note = self.run_action(card)
        self.assertIsNone(executed)
        self.assertTrue(note)
    def test_legacy_approval_without_baseline_fails_closed(self):
        card = self.request()
        payload = json.loads(card.action_payload)
        payload.pop('_approval_base', None)
        card.action_payload = json.dumps(payload)
        before = copy.deepcopy(self.order.items)
        executed, note = self.run_action(card)
        self.assertIsNone(executed)
        self.assertTrue(note)
        self.assertTrue(card.flags.stale_item_snapshot)
        self.assertEqual(self.order.items, before)
    def test_row_reordering_and_ids_do_not_invalidate_semantic_snapshot(self):
        card = self.request()
        self.order.items.reverse()
        self.order.items[0].name = 'different-row-id'
        executed, note = self.run_action(card)
        self.assertEqual(executed, 'items_updated')
        self.assertIsNone(note)
    def test_takeaway_change_rejects(self):
        card = self.request()
        self.order.items[1].takeaway = 1
        executed, note = self.run_action(card)
        self.assertIsNone(executed)
        self.assertTrue(note)
    def test_notes_change_rejects(self):
        card = self.request()
        self.order.items[1].notes = 'No salt'
        executed, note = self.run_action(card)
        self.assertIsNone(executed)
        self.assertTrue(note)
    def test_counter_rebuild_never_duplicates_fired_quantity(self):
        self.order.items = [line('FRIES', qty=3, rate=1690)]
        self.order.items[0].sent_qty = 2
        self.order.items[0].stock_consumed_qty = 3
        self.order.items[0].paid_qty = 1
        self.ns['_apply_order_items'](self.order.name, [{'item': 'FRIES', 'qty': 1}, {'item': 'FRIES', 'qty': 1}])
        self.assertEqual(sum(i.sent_qty for i in self.order.items), 2)
        self.assertEqual(sum(i.stock_consumed_qty for i in self.order.items), 3)
        self.assertEqual(sum(i.paid_qty for i in self.order.items), 1)
        self.assertTrue(all(i.sent_qty <= i.qty and i.paid_qty <= i.qty for i in self.order.items))
    def test_reduced_quantity_does_not_return_already_consumed_food(self):
        self.order.items = [line('FRIES', qty=3, rate=1690)]
        self.order.items[0].stock_consumed_qty = 3
        self.order.items[0].sent_qty = 3
        self.ns['_apply_order_items'](self.order.name, [{'item': 'FRIES', 'qty': 1}])
        self.assertEqual(len(self.order.items), 1)
        self.assertEqual(self.order.items[0].qty, 1)
        self.assertEqual(self.order.items[0].sent_qty, 1)
        self.assertEqual(self.order.items[0].stock_consumed_qty, 3)
    def test_stale_resolve_marks_superseded_so_old_pos_does_not_restore_cart(self):
        card = self.request()
        self.order.items.append(line('KOTTU'))
        self.frappe.local.form_dict = D(name=card.name, decision='approved')
        result = self.ns['resolve_approval']()
        self.assertEqual(result['status'], 'superseded')
        self.assertEqual(card.status, 'superseded')
        self.assertTrue(card.manager_comment)
        self.assertIsNone(result['executed'])
        self.assertEqual([i.item for i in self.order.items], ['SHAWARMA', 'FRIES', 'KOTTU'])
    def test_unchanged_approval_resolve_marks_approved(self):
        card = self.request()
        self.frappe.local.form_dict = D(name=card.name, decision='approved')
        result = self.ns['resolve_approval']()
        self.assertEqual(result['status'], 'approved')
        self.assertEqual(result['executed'], 'items_updated')
        self.assertIn(('POS Approval', card.name), self.db.locks)
        self.assertIn(('POS Order', self.order.name), self.db.locks)
    def test_partial_payment_after_request_cannot_be_rebuilt(self):
        card = self.request()
        self.order.items[1].paid_qty = 1
        executed, note = self.run_action(card)
        self.assertIsNone(executed)
        self.assertIn('partial payment', note)
        self.assertEqual(self.order.items[1].paid_qty, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
