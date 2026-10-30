"""Attribute legacy POS Invoices to the restaurant of their POS Profile.

Restaurant reports are separated with ERPNext's native ``cost_center`` field.
Older invoices created by the custom POS API carry ``pos_profile`` but did not
always receive that field, so they were correct sales with no branch to report
under.  The POS Profile is an unambiguous and permanent attribution source.
"""

import frappe


def execute():
    if not (frappe.db.has_column("POS Profile", "restaurant")
            and frappe.db.has_column("POS Invoice", "cost_center")):
        return

    frappe.db.sql("""
        UPDATE `tabPOS Invoice` pi
        INNER JOIN `tabPOS Profile` pp ON pp.name = pi.pos_profile
        LEFT JOIN `tabRestaurant` r ON r.name = pp.restaurant
        SET pi.cost_center = COALESCE(NULLIF(r.cost_center, ''),
                                     NULLIF(pp.cost_center, ''))
        WHERE IFNULL(pi.cost_center, '') = ''
          AND COALESCE(NULLIF(r.cost_center, ''),
                       NULLIF(pp.cost_center, '')) IS NOT NULL
    """)

    # Keep the submitted document internally consistent.  Manager reports
    # filter through the parent invoice, while native item/accounting views may
    # read the dimension from its rows.
    if frappe.db.has_column("POS Invoice Item", "cost_center"):
        frappe.db.sql("""
            UPDATE `tabPOS Invoice Item` item
            INNER JOIN `tabPOS Invoice` pi ON pi.name = item.parent
            SET item.cost_center = pi.cost_center
            WHERE IFNULL(item.cost_center, '') = ''
              AND IFNULL(pi.cost_center, '') != ''
        """)
