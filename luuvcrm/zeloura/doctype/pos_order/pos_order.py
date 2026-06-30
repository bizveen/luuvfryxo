import frappe
from frappe.model.document import Document

class POSOrder(Document):
    def validate(self):
        if not self.waiter_name:
            self.waiter_name = frappe.session.user
        if self.items:
            subtotal = 0
            for item in self.items:
                subtotal += (item.qty or 1) * (item.rate or 0)
            sc_rate = float(self.get("service_charge_rate") or 0)
            if sc_rate > 0:
                self.service_charge_amount = round(subtotal * sc_rate / 100, 2)
                self.grand_total = subtotal + self.service_charge_amount
            else:
                self.grand_total = subtotal