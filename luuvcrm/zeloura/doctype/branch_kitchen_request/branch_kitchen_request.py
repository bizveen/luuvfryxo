from frappe import _
import frappe
from frappe.model.document import Document
from frappe.utils import flt, now_datetime

from luuvcrm import branch


class BranchKitchenRequest(Document):
    """Food/prep requested by one restaurant from another restaurant's kitchen."""

    def before_insert(self):
        # Never trust a browser-supplied source branch. The logged-in user's
        # assignment/selection is the only branch allowed to raise a request.
        active = branch.current()
        if not active:
            frappe.throw(_("Select a restaurant before creating a kitchen request"))
        self.requesting_restaurant = active
        self.status = "Requested"
        self.requested_by = frappe.session.user
        self.requested_at = now_datetime()
        self.last_updated_by = frappe.session.user
        self.last_updated_at = self.requested_at
        self.append("status_history", {
            "status": self.status,
            "changed_by": frappe.session.user,
            "changed_at": self.requested_at,
            "note": self.notes or "",
        })

    def validate(self):
        if not self.requesting_restaurant or not frappe.db.exists(
                "Restaurant", self.requesting_restaurant):
            frappe.throw(_("A valid requesting restaurant is required"))
        if not self.supplying_restaurant or not frappe.db.exists(
                "Restaurant", self.supplying_restaurant):
            frappe.throw(_("A valid supplying restaurant is required"))
        if self.requesting_restaurant == self.supplying_restaurant:
            frappe.throw(_("Requesting and supplying restaurants must be different"))
        if not self.items:
            frappe.throw(_("Add at least one item"))

        for row in self.items:
            if not row.item or not frappe.db.exists("Item", row.item):
                frappe.throw(_("Every request line must contain a valid item"))
            if flt(row.qty) <= 0:
                frappe.throw(_("Quantity must be greater than zero for {0}").format(
                    row.item_name or row.item))
            if not row.item_name or not row.uom:
                item = frappe.db.get_value(
                    "Item", row.item, ["item_name", "stock_uom"], as_dict=True)
                row.item_name = row.item_name or item.item_name or row.item
                row.uom = row.uom or item.stock_uom
