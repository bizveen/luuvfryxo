"""Persist branch rules and check POS consumption under row locks."""
import frappe
from frappe import _
from frappe.utils import flt

from luuvcrm import activity
from luuvcrm.stock_policy import normalize, assess, rejection

FIELD = 'luuv_pos_stock_policy'


def current(restaurant, lock=False):
    if lock: frappe.db.sql('SELECT name FROM tabRestaurant WHERE name=%s FOR UPDATE',(restaurant,))
    raw = frappe.db.get_value('Restaurant', restaurant, FIELD) if frappe.db.has_column('Restaurant', FIELD) else None
    return normalize(frappe.parse_json(raw) if raw else None)


@frappe.whitelist(methods=['POST'])
def save(policy, restaurant=None):
    from luuvcrm.api.manager_inventory import _context
    restaurant, warehouse = _context(restaurant)
    if not frappe.db.has_column('Restaurant', FIELD):
        frappe.throw(_('Install the branch stock rules schema first'))
    before = current(restaurant, lock=True)
    try: after = normalize(frappe.parse_json(policy))
    except ValueError as exc: frappe.throw(str(exc))
    if after['revision'] != before['revision']:
        frappe.throw(_('Stock rules changed on another screen. Reload before saving.'))
    valid = set(frappe.get_all('Item', filters={'is_stock_item':1,'disabled':0},pluck='name',limit_page_length=0))
    if set(after['items']) - valid: frappe.throw(_('Choose enabled stock ingredients for the rules'))
    after['revision'] += 1
    frappe.db.set_value('Restaurant',restaurant,FIELD,frappe.as_json(after))
    activity.log_activity('stock.policy',label='POS stock rules updated: '+restaurant,
        ref_doctype='Restaurant',ref_name=restaurant,
        detail=dict(restaurant=restaurant,warehouse=warehouse,before=before,after=after))
    return after


def balance(item, warehouse):
    # Item locking also covers the first sale when no Bin exists yet.
    frappe.db.sql('SELECT name FROM tabItem WHERE name=%s FOR UPDATE',(item,))
    rows=frappe.db.sql('SELECT actual_qty FROM tabBin WHERE item_code=%s AND warehouse=%s FOR UPDATE',
                       (item,warehouse),as_dict=True)
    return flt(rows[0].actual_qty) if rows else 0


def check_sale(restaurant, warehouse, components, units):
    policy=current(restaurant,lock=True)
    rows=[]
    for item,quantity in sorted(components.items()):
        rows.append(dict(item=item,name=frappe.db.get_value('Item',item,'item_name') or item,
            uom=units[item],before=balance(item,warehouse),qty=quantity))
    result=assess(policy,rows)
    if result['blocked']: frappe.throw(rejection(result['blocked']))
    for row in result['warnings']:
        tracked=frappe.db.get_value('Item',row['item'],['has_batch_no','has_serial_no'],as_dict=True)
        if tracked and (tracked.has_batch_no or tracked.has_serial_no):
            frappe.throw(_('Batch or serial tracked stock cannot go negative: {0}').format(row['name']))
    return dict(policy=policy,warnings=result['warnings'],ingredients=rows)
