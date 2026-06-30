import frappe
from frappe.model.document import Document

class POSOrder(Document):
    def validate(self):
        if not self.waiter_name:
            self.waiter_name = frappe.session.user
        if self.items:
            subtotal = 0
            sc_base = 0  # service charge applies to dine-in items only (not take-away)
            for item in self.items:
                line = (item.qty or 1) * (item.rate or 0)
                subtotal += line
                if not item.get("takeaway"):
                    sc_base += line
            sc_rate = float(self.get("service_charge_rate") or 0)
            if sc_rate > 0:
                self.service_charge_amount = round(sc_base * sc_rate / 100, 2)
                self.grand_total = subtotal + self.service_charge_amount
            else:
                self.grand_total = subtotal