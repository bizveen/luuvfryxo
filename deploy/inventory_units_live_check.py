"""Read-only verification of the live unit editor and unchanged records."""
from unittest.mock import patch
import frappe
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
try:
    from luuvcrm import branch
    from luuvcrm.api import manager_inventory as manager,inventory_units as units
    with patch.object(branch,'selected',return_value='Kurunegala'):
        data=manager.dashboard('Kurunegala')
        assert data['uoms'] and all(frappe.db.get_value('UOM',u,'enabled') for u in data['uoms'])
        rows=data['stock']+data['main_inventory']
        for row in rows[:3]:
            info=manager.inventory_item_units(row['item'],'Kurunegala')
            assert info['unit']==row['stock_uom'] and info['can_change']==(not info['reasons'])
        print('PASS live branch unit metadata and enabled-unit choices')
    assert not frappe.db.exists('Item',{'item_name':['like','LFUNIT-%']})
    assert not frappe.db.exists('Purchase Bill',{'vendor':['like','LFUNIT-%']})
    print('PASS native test items and bills absent')
finally:frappe.db.rollback();frappe.destroy()
