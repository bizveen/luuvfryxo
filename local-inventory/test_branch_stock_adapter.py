"""Check branch scoping and per-document ERPNext flags without a live write."""
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def module_at(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


class BranchAdapterTest(unittest.TestCase):
    def setUp(self):
        self.values={};self.active='Kurunegala';self.is_manager=True;self.tracked=False
        self.frappe=ModuleType('frappe');self.frappe._=lambda s:s
        self.frappe.whitelist=lambda **kw:lambda fn:fn
        self.frappe.as_json=json.dumps;self.frappe.parse_json=lambda s:json.loads(s) if isinstance(s,str) else s
        self.frappe.throw=lambda s:(_ for _ in ()).throw(ValueError(s))
        def get_value(dt,name,field,**kw):
            if dt=='Restaurant':return self.values.get(name)
            if field=='item_name':return 'Chicken'
            return SimpleNamespace(has_batch_no=self.tracked,has_serial_no=False)
        def set_value(dt,name,field,value):self.values[name]=value
        self.frappe.db=SimpleNamespace(has_column=lambda *a:True,
            sql=Mock(return_value=[SimpleNamespace(actual_qty=.135)]),get_value=get_value,set_value=set_value)
        self.frappe.get_all=lambda *a,**kw:['CHICKEN']
        activity=ModuleType('luuvcrm.activity');activity.log_activity=Mock();self.activity=activity
        manager=ModuleType('luuvcrm.api.manager_inventory')
        def context(restaurant):
            if not self.is_manager:raise ValueError('Manager permission required')
            if restaurant and restaurant!=self.active:raise ValueError('Switch branch')
            return self.active,self.active+' warehouse'
        manager._context=context
        api=ModuleType('luuvcrm.api');api.__path__=[]
        utils=ModuleType('frappe.utils');utils.flt=lambda value:float(value or 0)
        self.mock_modules=patch.dict(sys.modules,{'frappe':self.frappe,'frappe.utils':utils,
            'luuvcrm.activity':activity,'luuvcrm.api':api,'luuvcrm.api.manager_inventory':manager})
        self.mock_modules.start();self.addCleanup(self.mock_modules.stop)
        self.policy=module_at('policy_adapter_test','luuvcrm/api/branch_stock_policy.py')

    def test_policies_are_per_branch_and_edits_are_versioned(self):
        kg=self.policy.save(dict(default=dict(mode='negative')),'Kurunegala')
        self.active='Other branch'
        other=self.policy.save(dict(default=dict(mode='percent',percent=20)),'Other branch')
        self.assertEqual(self.policy.current('Kurunegala'),kg)
        self.assertEqual(self.policy.current('Other branch'),other)
        with self.assertRaisesRegex(ValueError,'another screen'):self.policy.save(dict(default=dict(mode='block')),'Other branch')
        self.assertEqual(self.activity.log_activity.call_count,2)

    def test_only_active_branch_manager_can_save(self):
        with self.assertRaisesRegex(ValueError,'Switch branch'):self.policy.save({},'Other branch')
        self.is_manager=False
        with self.assertRaisesRegex(ValueError,'Manager permission'):self.policy.save({},'Kurunegala')
        self.assertEqual(self.values,{})

    def test_preflight_uses_locked_bin_and_returns_rule_snapshot(self):
        self.policy.save(dict(items={'CHICKEN':dict(mode='percent',percent=10)}),'Kurunegala')
        result=self.policy.check_sale('Kurunegala','KG warehouse',{'CHICKEN':.15},{'CHICKEN':'Kg'})
        self.assertEqual(result['warnings'][0]['after'],-.015)
        queries=[call.args[0] for call in self.frappe.db.sql.call_args_list]
        self.assertTrue(any('tabBin' in sql and 'FOR UPDATE' in sql for sql in queries))
        self.assertTrue(any('tabRestaurant' in sql and 'FOR UPDATE' in sql for sql in queries))
        self.assertEqual(result['policy']['revision'],1)

    def test_tracked_negative_stock_is_not_bypassed(self):
        self.policy.save(dict(default=dict(mode='negative')),'Kurunegala');self.tracked=True
        with self.assertRaisesRegex(ValueError,'Batch or serial'):
            self.policy.check_sale('Kurunegala','KG warehouse',{'CHICKEN':.15},{'CHICKEN':'Kg'})


class EntryAllowanceTest(unittest.TestCase):
    def test_allowance_is_per_document_and_keeps_native_default(self):
        class Base:
            def __init__(self):
                self.flags=SimpleNamespace(luuv_checked_negative_stock=False)
                self.posting_date='2026-11-11';self.posting_time='10:00:00'
                self.items=[SimpleNamespace(item_code='CHICKEN',s_warehouse='KG',t_warehouse=None)]
            def get(self,key):return getattr(self,key)
            def set_actual_qty(self):self.native_qty_check=True
            def update_stock_ledger(self,allow_negative_stock=False):return allow_negative_stock
        entry_module=ModuleType('erpnext.stock.doctype.stock_entry.stock_entry');entry_module.StockEntry=Base
        reconciliation=ModuleType('erpnext.stock.doctype.stock_reconciliation.stock_reconciliation');reconciliation.StockReconciliation=Base
        ledger=ModuleType('erpnext.stock.stock_ledger');ledger.get_previous_sle=lambda _:dict(qty_after_transaction=.135)
        with patch.dict(sys.modules,{'erpnext.stock.doctype.stock_entry.stock_entry':entry_module,
                'erpnext.stock.doctype.stock_reconciliation.stock_reconciliation':reconciliation,'erpnext.stock.stock_ledger':ledger}):
            target=module_at('entry_adapter_test','luuvcrm/branch_stock_entry.py')
            normal=target.BranchStockEntry();normal.set_actual_qty()
            self.assertTrue(normal.native_qty_check);self.assertFalse(normal.update_stock_ledger())
            allowed=target.BranchStockEntry();allowed.flags.luuv_checked_negative_stock=True;allowed.set_actual_qty()
            self.assertEqual(allowed.items[0].actual_qty,.135);self.assertTrue(allowed.update_stock_ledger())
            self.assertFalse(normal.update_stock_ledger())
            audit=target.BranchStockReconciliation();audit.flags.luuv_correcting_negative_stock=False
            self.assertFalse(audit.update_stock_ledger())
            audit.flags.luuv_correcting_negative_stock=True
            self.assertTrue(audit.update_stock_ledger())
            self.assertFalse(normal.update_stock_ledger())


if __name__=='__main__':unittest.main()
