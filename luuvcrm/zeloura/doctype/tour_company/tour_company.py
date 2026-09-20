# Copyright (c) LuuvCRM
# For license information, please see license.txt

import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

#: Off the menu price. The rate card is a courtesy to a tour company, not a
#: price list, and a typed 100 is far likelier to be a slip than a policy.
MAX_DISCOUNT = 50.0


def parse_items(raw):
    """A list of item codes from whatever was stored or sent: a JSON string or a
    list. Blank and repeated codes are dropped, order is kept."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except ValueError:
            raw = []
    seen, codes = set(), []
    for c in raw if isinstance(raw, list) else []:
        c = str(c).strip()
        if c and c not in seen:
            seen.add(c)
            codes.append(c)
    return codes


class TourCompany(Document):
    """A tour or transport company the restaurant serves.

    Named for what it is so nobody reads it as the ERPNext Company, which is the
    restaurant itself. The one rule that lives here rather than in an endpoint is
    the discount's range, so a row typed into the Desk obeys it too.
    """

    def before_naming(self):
        # The name is the company name, and it is made before validate() runs:
        # tidy it here or "  Blue   Wave " becomes the record's name.
        self.company_name = " ".join((self.company_name or "").split())

    def validate(self):
        self.company_name = " ".join((self.company_name or "").split())
        if not self.company_name:
            frappe.throw(_("Enter the company name"))
        if len(self.company_name) > 80:
            frappe.throw(_("The company name is too long"))
        self.discount_percent = flt(self.discount_percent)
        if not 0 <= self.discount_percent <= MAX_DISCOUNT:
            frappe.throw(_("The discount must be between 0 and {0}%").format(int(MAX_DISCOUNT)))
        codes = parse_items(self.items)
        if not codes:
            frappe.throw(_("Pick at least one item for the rate card"))
        self.items = json.dumps(codes)
