# Copyright (c) LuuvCRM
# For license information, please see license.txt

from frappe.model.document import Document
from frappe.utils import getdate, today


class PurchaseBill(Document):
	def validate(self):
		# Every purchase report windows on purchase_date, so a bill without one
		# would fall out of all of them. A bill that arrives without it — from the
		# desk, an old client, a test — is dated the day it was entered.
		if not self.purchase_date:
			self.purchase_date = getdate(self.requested_at) if self.requested_at else today()
