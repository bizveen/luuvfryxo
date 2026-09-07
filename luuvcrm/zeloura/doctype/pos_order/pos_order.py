import frappe
from frappe.model.document import Document

class POSOrder(Document):
    def validate(self):
        if not self.waiter_name:
            self.waiter_name = frappe.session.user
        if self.items:
            # Service charge is on the dine-in lines only. A parcelled line is
            # not table service, so it does not carry it.
            #
            # api._apply_takeaway_service_charge() recomputes this after every
            # insert and save, and is still what the totals actually come from —
            # it also applies the approved-discount factor, which does not belong
            # in a controller. This is here so that a POS Order saved by anything
            # that is not that endpoint — the Desk, a future path — is not wrong
            # in the restaurant's favour in the meantime. The two agree now; they
            # did not before, because this logic existed only in the duplicate
            # DocType folder that Frappe never loaded.
            subtotal = 0
            sc_base = 0
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