"""Bridge manager stock to existing Purchase Bills; never post a second receipt."""
import frappe
from frappe import _
from frappe.utils import flt

from luuvcrm import branch, roles
from luuvcrm.api import inventory
from luuvcrm.api.expenses import category_is_stock
from luuvcrm.inventory_planning import low_stock


def pending_quantities(restaurant, catalog):
    """Only pending stock lines in this branch, converted to the Item stock UOM."""
    if not frappe.db.has_column('Purchase Bill', 'restaurant'):
        return {}
    by_item = {row['item']: row for row in catalog}
    rows = frappe.db.sql('''SELECT i.item, i.qty, i.uom, i.category
        FROM `tabPurchase Bill Item` i JOIN `tabPurchase Bill` b ON b.name=i.parent
        WHERE i.parenttype='Purchase Bill' AND b.restaurant=%s AND b.status='pending' ''',
        (restaurant,), as_dict=True)
    pending = {}
    for row in rows:
        item = by_item.get(row.item)
        if not item or not category_is_stock(row.category) or flt(row.qty) <= 0:
            continue
        conversion = 1
        if row.uom and row.uom != item['stock_uom']:
            from erpnext.stock.get_item_details import get_conversion_factor
            conversion = flt(get_conversion_factor(row.item, row.uom).get('conversion_factor'))
        # An unconvertible pending bill cannot be counted as usable stock.
        if conversion > 0:
            pending[row.item] = pending.get(row.item, 0) + flt(row.qty) * conversion
    return pending


def prepared_codes(restaurant):
    """Explicit branch recipes only; do not infer branch prep from central BOMs."""
    if not frappe.db.has_column('BOM', 'luuv_recipe_restaurant'):
        return set()
    from luuvcrm.api.manager_inventory import _menu
    return set(frappe.get_all('BOM', filters={'luuv_recipe_restaurant': restaurant,
        'docstatus': ['<', 2]}, pluck='item', limit_page_length=0)) - set(_menu(restaurant))


@frappe.whitelist()
def restock(restaurant=None):
    roles.require_purchases()
    restaurant = inventory._active_restaurant(restaurant)
    warehouse = branch.warehouse(restaurant) if restaurant else None
    if not warehouse:
        frappe.throw(_('Select a branch with a stock warehouse first'))
    from luuvcrm.api.manager_inventory import _catalog
    catalog = _catalog(warehouse)
    prepared = prepared_codes(restaurant)
    for row in catalog:
        row['prepared'] = row['item'] in prepared
    categories = frappe.get_all('Expense Category', filters={'in_stock': 1,
        'has_item_list': 1, 'disabled': 0}, pluck='name', order_by='name', limit_page_length=0)
    return {'restaurant': restaurant, 'warehouse': warehouse,
            'stock_categories': categories,
            'items': low_stock(catalog, pending_quantities(restaurant, catalog))}
