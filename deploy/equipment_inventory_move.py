"""Reviewed Kurunegala equipment list. Read-only unless APPLY_EQUIPMENT is set."""
import json
from unittest.mock import patch
import frappe

EQUIPMENT = {
    'Bowl': 'Kitchen Equipment',
    'Container': 'Kitchen Equipment',
    'Food tong': 'Kitchen Equipment',
    'Food tong thuva': 'Kitchen Equipment',
    'Grater slicer': 'Kitchen Equipment',
    'Mug': 'Kitchen Equipment',
    'Scissors': 'Kitchen Equipment',
    'Water jug': 'Kitchen Equipment',
    'Steplar machine': 'Office Equipment',
    'Light': 'Electrical Equipment',
}
apply = bool(globals().get('APPLY_EQUIPMENT', False))
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
try:
    from luuvcrm import branch
    from luuvcrm.api import inventory_sections as sections, manager_inventory as manager
    warehouse=branch.warehouse('Kurunegala')
    names=sorted(EQUIPMENT)
    def snapshot():
        return dict(
            bins=frappe.get_all('Bin',filters={'warehouse':warehouse,'item_code':['in',names]},fields=['name','item_code','actual_qty','valuation_rate','stock_value'],order_by='item_code'),
            items=frappe.get_all('Item',filters={'name':['in',names]},fields=['name','is_stock_item','is_fixed_asset','stock_uom','item_group'],order_by='name'),
            bills=frappe.get_all('Purchase Bill Item',filters={'item':['in',names]},fields=['name','parent','category','qty','price','item','asset'],order_by='name'),
            ledger=frappe.get_all('Stock Ledger Entry',filters={'item_code':['in',names],'warehouse':warehouse},fields=['name','actual_qty','stock_value_difference','voucher_no','is_cancelled'],order_by='name'))
    with patch.object(branch,'selected',return_value='Kurunegala'):
        if apply:
            sections.settings(warehouse,lock=True)
            from luuvcrm.api.branch_stock_policy import balance
            for item in names: balance(item,warehouse)
        before=snapshot()
        assert len(before['items'])==len(names) and len(before['bins'])==len(names)
        config=sections.settings(warehouse)
        plan=[dict(item=r.item_code,qty=float(r.actual_qty),category=EQUIPMENT[r.item_code],
                   current='main' if r.item_code in config['items'] else 'running') for r in before['bins']]
        print('Equipment list:',json.dumps(plan),flush=True)
        if not apply:
            print('PLAN ONLY: quantities, purchases and stock ledger remain unchanged')
        else:
            for item in names:
                current=sections.settings(warehouse)
                manager.move_inventory_item(item,'main',current['revision'],EQUIPMENT[item],
                    'Reusable branch equipment moved to Main inventory as requested; quantity retained.','Kurunegala')
            after=snapshot()
            assert before==after,'Moving equipment must not change quantity, value, purchase bills or stock history'
            data=manager.dashboard('Kurunegala')
            assert not set(names)&{r['item'] for r in data['stock']}
            assert set(names)<={r['item'] for r in data['main_inventory'] if r['source']=='stock'}
            result=dict(warehouse=warehouse,moved=plan,running_items=len(data['stock']),
                main_records=len(data['main_inventory']),registered_assets=sum(r['source']=='asset' for r in data['main_inventory']),
                before=before,after=after)
            frappe.db.commit()
            print('AUDIT_JSON:'+json.dumps(result,default=str))
            print('COMMITTED: '+str(len(names))+' equipment types separated; '+str(result['running_items'])+' running items; '+str(result['registered_assets'])+' registered asset records visible')
finally:frappe.db.rollback();frappe.destroy()
