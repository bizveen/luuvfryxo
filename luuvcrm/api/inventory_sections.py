"""Branch equipment classification. Moving a list entry never posts stock or GL."""
import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

from luuvcrm import activity, branch

FIELD = 'luuv_main_inventory'
GROUPS = ('Kitchen Equipment', 'Electrical Equipment', 'Furniture & Fixtures', 'Office Equipment', 'Other Equipment')


def settings(warehouse, lock=False):
    if lock:
        frappe.db.sql('SELECT name FROM tabWarehouse WHERE name=%s FOR UPDATE', (warehouse,))
    raw = frappe.db.get_value('Warehouse', warehouse, FIELD) if warehouse and frappe.db.has_column('Warehouse', FIELD) else None
    data = frappe.parse_json(raw) if raw else {}
    return dict(revision=cint(data.get('revision')), items=data.get('items') or {})


def main_items(warehouse):
    return set(settings(warehouse)['items'])


def assets(restaurant, warehouse):
    company = frappe.db.get_value('Warehouse', warehouse, 'company')
    result = []
    for row in frappe.get_all('Asset', filters={'company': company,
            'location': restaurant+' Restaurant', 'docstatus': ['<', 2]},
            fields=['name', 'item_code', 'asset_name', 'asset_category', 'asset_quantity', 'status'],
            order_by='asset_name', limit_page_length=0):
        if row.status in ('Sold', 'Scrapped'):
            continue
        item = frappe.db.get_value('Item', row.item_code, ['item_name', 'stock_uom'], as_dict=True) or {}
        result.append(dict(item=row.item_code, name=item.get('item_name') or row.asset_name,
            stock_uom=item.get('stock_uom') or 'Nos', balance=flt(row.asset_quantity),
            equipment_category=row.asset_category, source='asset', asset=row.name, status=row.status))
    return result


def view(restaurant, warehouse, stock):
    config = settings(warehouse)
    rows = [dict(r, source='stock', equipment_category=config['items'][r['item']]['category'],
                 section_note=config['items'][r['item']].get('note', ''))
            for r in stock if r['item'] in config['items']]
    rows.extend(assets(restaurant, warehouse))
    return dict(main_inventory=sorted(rows, key=lambda r: r['name'].casefold()),
                section_revision=config['revision'], equipment_categories=list(GROUPS))


def move(item_code, section, expected_revision, category='', comment='', restaurant=None):
    from luuvcrm.api.manager_inventory import _context, _menu
    restaurant, warehouse = _context(restaurant)
    if not frappe.db.has_column('Warehouse', FIELD):
        frappe.throw(_('Install the inventory sections update first.'))
    if section not in ('main', 'running'):
        frappe.throw(_('Choose Main inventory or Running stock.'))
    comment = str(comment or '').strip()
    if not 3 <= len(comment) <= 500:
        frappe.throw(_('Add a short reason for this move (3–500 characters).'))
    if section == 'main' and category not in GROUPS:
        frappe.throw(_('Choose an equipment category.'))
    config = settings(warehouse, lock=True)
    if str(expected_revision) != str(config['revision']):
        frappe.throw(_('Inventory sections changed. Refresh and try again.'))
    item = frappe.db.get_value('Item', item_code, ['item_name', 'is_stock_item', 'is_fixed_asset', 'disabled'], as_dict=True)
    if not item or not item.is_stock_item or item.is_fixed_asset or item.disabled:
        frappe.throw(_('Choose an enabled stock item. Registered assets stay in Main inventory.'))
    # A branch may classify only its own stock records.
    minimums = frappe.parse_json(frappe.db.get_value('Warehouse', warehouse, 'luuv_stock_minimums') or '{}')
    if item_code not in config['items'] and item_code not in minimums and not frappe.db.exists('Bin', {'warehouse': warehouse, 'item_code': item_code}):
        frappe.throw(_('This item is not in this branch inventory.'), frappe.PermissionError)
    old = config['items'].get(item_code)
    before = 'main' if old else 'running'
    if section == before and (section == 'running' or old['category'] == category):
        return dict(ok=True, changed=False, revision=config['revision'])
    if section == 'main':
        used = frappe.db.sql('''SELECT b.name FROM tabBOM b LEFT JOIN `tabBOM Item` i ON i.parent=b.name
            WHERE b.docstatus<2 AND (b.is_active=1 OR b.docstatus=0)
              AND (b.luuv_recipe_restaurant=%s OR COALESCE(b.luuv_recipe_restaurant,'')='')
              AND (b.item=%s OR i.item_code=%s) LIMIT 1''', (restaurant, item_code, item_code))
        if used or item_code in _menu(restaurant):
            frappe.throw(_('This item is used by a menu or recipe. Review that recipe before moving it to Main inventory.'))
        config['items'][item_code] = dict(category=category, note=comment,
                                        by=frappe.session.user, when=str(now_datetime()))
    else:
        config['items'].pop(item_code, None)
    config['revision'] += 1
    record = dict(item=item_code, name=item.item_name, restaurant=restaurant, warehouse=warehouse,
                  before=before, after=section, previous_category=(old or {}).get('category'),
                  category=category if section == 'main' else '', comment=comment, revision=config['revision'])
    frappe.db.set_value('Warehouse', warehouse, FIELD, frappe.as_json(config))
    frappe.get_doc(dict(doctype='Version', ref_doctype='Warehouse', docname=warehouse,
        data=frappe.as_json({'inventory_section_move': record}))).insert(ignore_permissions=True)
    activity.log_activity('inventory.section', label='Inventory section changed: '+item.item_name,
        ref_doctype='Item', ref_name=item_code, item=item_code, detail=record)
    return dict(ok=True, changed=True, revision=config['revision'])
