"""Production bridge checks with a small read-only Frappe stand-in."""
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


class Row(dict):
    __getattr__=dict.get


class ProductionBridgeTest(unittest.TestCase):
    def setUp(self):
        self.frappe=ModuleType('frappe');self.frappe._=lambda text:text
        self.frappe.whitelist=lambda **kw:lambda fn:fn
        self.frappe.db=SimpleNamespace(has_column=Mock(return_value=True),sql=Mock())
        self.frappe.get_all=Mock(return_value=['Food / Ingredients'])
        self.frappe.throw=lambda text:(_ for _ in ()).throw(ValueError(text))
        roles=ModuleType('luuvcrm.roles');roles.require_purchases=Mock()
        self.roles=roles
        branch=ModuleType('luuvcrm.branch');branch.warehouse=Mock(return_value='KG warehouse')
        inventory=ModuleType('luuvcrm.api.inventory')
        def active(restaurant):
            if restaurant and restaurant!='Kurunegala':raise ValueError('Switch branch')
            return 'Kurunegala'
        inventory._active_restaurant=active
        expenses=ModuleType('luuvcrm.api.expenses');expenses.category_is_stock=lambda c:c in ('Food / Ingredients','')
        utils=ModuleType('frappe.utils');utils.flt=lambda n:float(n or 0)
        item_details=ModuleType('erpnext.stock.get_item_details')
        item_details.get_conversion_factor=lambda item,unit:dict(conversion_factor={'ml':.001,'g':.001}.get(unit,0))
        manager=ModuleType('luuvcrm.api.manager_inventory');manager._menu=lambda _:[]
        manager._catalog=lambda _:self.catalog
        self.catalog=[dict(item='HOT',name='Hot sauce',stock_uom='L',balance=0,minimum_qty=2)]
        api=ModuleType('luuvcrm.api');api.__path__=[];api.inventory=inventory
        modules={'luuvcrm.api':api,'frappe':self.frappe,'frappe.utils':utils,'luuvcrm.roles':roles,'luuvcrm.branch':branch,
            'luuvcrm.api.inventory':inventory,'luuvcrm.api.expenses':expenses,
            'luuvcrm.api.manager_inventory':manager,'erpnext.stock.get_item_details':item_details}
        self.patch=patch.dict(sys.modules,modules);self.patch.start();self.addCleanup(self.patch.stop)
        spec=importlib.util.spec_from_file_location('bridge_test_target',Path(__file__).resolve().parents[1]/'luuvcrm/api/inventory_purchasing.py')
        self.bridge=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.bridge)

    def test_pending_native_units_categories_and_branch_filter(self):
        self.frappe.db.sql.return_value=[Row(item='HOT',qty=500,uom='ml',category='Food / Ingredients'),
            Row(item='HOT',qty=2,uom='L',category='Staff Meals'),
            Row(item='HOT',qty=1,uom='Box',category='Food / Ingredients')]
        result=self.bridge.pending_quantities('Kurunegala',self.catalog)
        self.assertEqual(result,{'HOT':.5})
        query,args=self.frappe.db.sql.call_args.args
        self.assertIn("b.status='pending'",query);self.assertIn('b.restaurant=%s',query)
        self.assertEqual(args,('Kurunegala',))

    def test_role_and_active_branch_checked_before_stock_read(self):
        with self.assertRaisesRegex(ValueError,'Switch branch'):self.bridge.restock('Other')
        self.roles.require_purchases.assert_called_once()
        self.frappe.db.sql.assert_not_called()

    def test_restock_returns_only_enabled_stock_categories(self):
        self.frappe.db.sql.return_value=[]
        with patch.object(self.bridge,'prepared_codes',return_value=set()):data=self.bridge.restock('Kurunegala')
        self.assertEqual(data['warehouse'],'KG warehouse')
        self.assertEqual(data['items'][0]['suggested_qty'],2)
        self.assertEqual(self.frappe.get_all.call_args.kwargs['filters'],dict(in_stock=1,has_item_list=1,disabled=0))


if __name__=='__main__':unittest.main()
