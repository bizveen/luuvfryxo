"""Customer points, referrals, and the cashback balance.

Split out of the single 7,338-line api.py; every name here is re-exported
from `luuvcrm.api`, so the whitelisted method paths are unchanged.
"""

import frappe
import secrets
import random
import string

from luuvcrm.api.customers import _customer_mobile


# ─── Loyalty Programme Setup ──────────────────────────────

@frappe.whitelist()
def setup_loyalty():
    """Create loyalty DocTypes and Settings."""
    frappe.only_for("System Manager")
    created = []

    if not frappe.db.exists("DocType", "Loyalty Customer"):
        doc = frappe.get_doc({
            "doctype": "DocType", "name": "Loyalty Customer", "module": "Zeloura", "custom": 1,
            "fields": [
                {"fieldname": "customer_name", "label": "Customer Name", "fieldtype": "Data"},
                {"fieldname": "mobile", "label": "Mobile", "fieldtype": "Data", "reqd": 1, "unique": 1, "in_list_view": 1},
                {"fieldname": "referral_code", "label": "Referral Code", "fieldtype": "Data", "unique": 1},
                {"fieldname": "referred_by", "label": "Referred By", "fieldtype": "Link", "options": "Loyalty Customer"},
                {"fieldname": "cashback_balance", "label": "Cashback Balance", "fieldtype": "Currency", "default": 0},
                {"fieldname": "total_earned", "label": "Total Earned", "fieldtype": "Currency", "default": 0},
                {"fieldname": "total_redeemed", "label": "Total Redeemed", "fieldtype": "Currency", "default": 0},
            ],
            "permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
        })
        doc.insert()
        created.append("Loyalty Customer")

    if not frappe.db.exists("DocType", "Cashback Transaction"):
        doc = frappe.get_doc({
            "doctype": "DocType", "name": "Cashback Transaction", "module": "Zeloura", "custom": 1,
            "fields": [
                {"fieldname": "loyalty_customer", "label": "Loyalty Customer", "fieldtype": "Link", "options": "Loyalty Customer", "reqd": 1, "in_list_view": 1},
                {"fieldname": "type", "label": "Type", "fieldtype": "Select", "options": "Earn\nRedeem\nReferral Discount", "reqd": 1, "in_list_view": 1},
                {"fieldname": "level", "label": "Level", "fieldtype": "Int"},
                {"fieldname": "amount", "label": "Amount", "fieldtype": "Currency", "reqd": 1},
                {"fieldname": "reference_order", "label": "Reference Order", "fieldtype": "Data"},
                {"fieldname": "notes", "label": "Notes", "fieldtype": "Small Text"},
            ],
            "permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
        })
        doc.insert()
        created.append("Cashback Transaction")

    if not frappe.db.exists("DocType", "Loyalty Settings"):
        doc = frappe.get_doc({
            "doctype": "DocType", "name": "Loyalty Settings", "module": "Zeloura", "custom": 1, "issingle": 1,
            "fields": [
                {"fieldname": "level_1_percent", "label": "Level 1 (%)", "fieldtype": "Percent", "default": 5.0},
                {"fieldname": "level_2_percent", "label": "Level 2 (%)", "fieldtype": "Percent", "default": 2.0},
                {"fieldname": "level_3_percent", "label": "Level 3 (%)", "fieldtype": "Percent", "default": 1.0},
                {"fieldname": "min_redeem", "label": "Minimum Redeem (LKR)", "fieldtype": "Currency", "default": 200},
                {"fieldname": "referral_discount_percent", "label": "Referral Discount (%)", "fieldtype": "Percent", "default": 5.0},
                {"fieldname": "expiry_days", "label": "Expiry Days", "fieldtype": "Int", "default": 365},
            ],
            "permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
        })
        doc.insert()
        created.append("Loyalty Settings")

    # Ensure default settings record exists
    if not frappe.db.exists("Loyalty Settings", "Loyalty Settings"):
        s = frappe.get_doc({"doctype": "Loyalty Settings"})
        s.flags.ignore_permissions = True
        s.insert()

    return {"status": "created" if created else "already_exists", "created": created}
# ─── Loyalty Helpers ─────────────────────────────────────

# debug_loyalty was here: a guest-accessible endpoint that took a phone number and
# returned that customer's referral code, cashback balance, and the phone number of
# whoever referred them. The same enumeration hole as the customer_get_orders
# fallback closed in Phase 1 (S3), and no page ever called it. Removed rather than
# guarded — a debug endpoint is not something to leave lying around behind a role.

def _get_loyalty_customer(mobile):
    """Get or create a Loyalty Customer record for this mobile."""
    existing = frappe.db.get_value("Loyalty Customer", {"mobile": mobile}, "name")
    if existing:
        return existing
    # Create new Loyalty Customer
    code = _generate_referral_code()
    lc = frappe.get_doc({
        "doctype": "Loyalty Customer",
        "customer_name": "",
        "mobile": mobile,
        "referral_code": code,
        "cashback_balance": 0,
        "total_earned": 0,
        "total_redeemed": 0,
    })
    lc.flags.ignore_permissions = True
    lc.insert()
    frappe.db.commit()
    return lc.name
def _generate_referral_code():
    """Generate a unique 6-char referral code like LUUV7X."""
    for _ in range(50):
        code = "LV" + "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
        if not frappe.db.exists("Loyalty Customer", {"referral_code": code}):
            return code
    return "LV" + secrets.token_hex(3).upper()[:4]
def _get_loyalty_settings():
    """Get loyalty settings dict."""
    if frappe.db.exists("Loyalty Settings", "Loyalty Settings"):
        doc = frappe.get_doc("Loyalty Settings", "Loyalty Settings")
        return {
            "level_1_percent": float(doc.level_1_percent or 5.0),
            "level_2_percent": float(doc.level_2_percent or 2.0),
            "level_3_percent": float(doc.level_3_percent or 1.0),
            "min_redeem": float(doc.min_redeem or 200),
            "referral_discount_percent": float(doc.referral_discount_percent or 5.0),
        }
    return {"level_1_percent": 5.0, "level_2_percent": 2.0, "level_3_percent": 1.0, "min_redeem": 200, "referral_discount_percent": 5.0}
def _credit_cashback(lc_name, amount, level, ref_order="", notes=""):
    """Credit cashback to a Loyalty Customer and log transaction."""
    if not lc_name or amount <= 0:
        return
    # Credit balance
    lc = frappe.get_doc("Loyalty Customer", lc_name)
    lc.cashback_balance = (lc.cashback_balance or 0) + amount
    lc.total_earned = (lc.total_earned or 0) + amount
    lc.flags.ignore_permissions = True
    lc.save(ignore_permissions=True)

    # Log transaction
    txn = frappe.get_doc({
        "doctype": "Cashback Transaction",
        "loyalty_customer": lc_name,
        "type": "Earn",
        "level": level,
        "amount": amount,
        "reference_order": ref_order,
        "notes": notes,
    })
    txn.flags.ignore_permissions = True
    txn.insert()
    frappe.db.commit()
def _process_cashback(mobile, order_name, grand_total):
    """After an order, credit cashback up to 3 levels up the referral chain."""
    try:
        lc_name = _get_loyalty_customer(mobile)
        lc = frappe.get_doc("Loyalty Customer", lc_name)
        settings = _get_loyalty_settings()

        # Simple approach: get the referred_by chain directly
        current_ref = lc.get("referred_by")

        for level_idx, percent_key in enumerate(["level_1_percent", "level_2_percent", "level_3_percent"]):
            if current_ref:
                ref_lc = frappe.get_doc("Loyalty Customer", current_ref)
                amount = round(grand_total * settings[percent_key] / 100, 2)
                if amount > 0:
                    _credit_cashback(ref_lc.name, amount, level_idx + 1, order_name,
                                     f"Level {level_idx+1} cashback from order {order_name} by {mobile}")
                current_ref = ref_lc.get("referred_by")
            else:
                break
    except Exception as e:
        frappe.log_error(f"Cashback error: {str(e)}", "Cashback")
# ─── Loyalty API ─────────────────────────────────────────

@frappe.whitelist(allow_guest=True)
def loyalty_get_info():
    mobile = _customer_mobile()
    if not mobile:
        return {"success": False, "error": "Login required"}

    lc_name = frappe.db.get_value("Loyalty Customer", {"mobile": mobile}, "name")
    if not lc_name:
        return {"success": False, "error": "Not found", "has_account": False}

    lc = frappe.get_doc("Loyalty Customer", lc_name)
    settings = _get_loyalty_settings()

    # Get direct referrals (Level 1 downline)
    downline = frappe.get_all("Loyalty Customer", filters={"referred_by": lc_name},
                              fields=["customer_name", "mobile", "total_earned", "creation"])
    downline_data = [{"name": d.customer_name or d.mobile, "mobile": d.mobile, "total_earned": d.total_earned or 0}
                     for d in downline]

    return {
        "success": True,
        "customer_name": lc.customer_name or "",
        "mobile": lc.mobile,
        "referral_code": lc.referral_code or "",
        "cashback_balance": lc.cashback_balance or 0,
        "total_earned": lc.total_earned or 0,
        "total_redeemed": lc.total_redeemed or 0,
        "downline_count": len(downline_data),
        "downline": downline_data,
        "settings": settings,
    }
@frappe.whitelist(allow_guest=True)
def loyalty_generate_code():
    mobile = _customer_mobile()
    if not mobile:
        return {"success": False, "error": "Login required"}

    lc_name = _get_loyalty_customer(mobile)
    lc = frappe.get_doc("Loyalty Customer", lc_name)
    if lc.referral_code:
        return {"success": True, "referral_code": lc.referral_code}

    lc.referral_code = _generate_referral_code()
    lc.flags.ignore_permissions = True
    lc.save(ignore_permissions=True)
    return {"success": True, "referral_code": lc.referral_code}
@frappe.whitelist(allow_guest=True)
def loyalty_apply_referral():
    """Link a customer to whoever referred them. Once, and only once.

    The token is preferred, but a posted mobile is still accepted: a customer
    can check out from /cus without an account, and the referral has to attach
    before the order or the referrer earns nothing on it.

    Unlike the rest of the loyalty endpoints this one only WRITES a link — it
    reads no balance and spends nothing — and the "already linked" guard below
    makes it one-shot per customer. The residual risk is somebody claiming
    referral credit for a number that has not been referred yet, which is
    business fraud rather than a data breach; closing it needs the guest
    checkout to carry a signed link (docs/ROADMAP.md).
    """
    mobile = _customer_mobile() or frappe.local.form_dict.get("mobile", "").strip()
    ref_code = frappe.local.form_dict.get("ref_code", "").strip().upper()

    if not mobile:
        return {"success": False, "error": "Login required"}
    if not ref_code:
        return {"success": False, "error": "Referral code required"}

    # Find referrer by code
    referrer = frappe.db.get_value("Loyalty Customer", {"referral_code": ref_code}, "name")
    if not referrer:
        return {"success": False, "error": "Invalid referral code"}

    ref_doc = frappe.get_doc("Loyalty Customer", referrer)
    if ref_doc.mobile == mobile:
        return {"success": False, "error": "Cannot refer yourself"}

    # Get or create customer
    lc_name = _get_loyalty_customer(mobile)
    lc = frappe.get_doc("Loyalty Customer", lc_name)

    if lc.referred_by:
        return {"success": False, "error": "Already linked to a referrer"}

    lc.referred_by = referrer
    lc.flags.ignore_permissions = True
    lc.save(ignore_permissions=True)
    frappe.db.commit()

    return {"success": True, "referrer": ref_doc.customer_name or ref_doc.mobile}
@frappe.whitelist(allow_guest=True)
def loyalty_redeem():
    mobile = _customer_mobile()
    amount = float(frappe.local.form_dict.get("amount", 0))

    if not mobile:
        return {"success": False, "error": "Login required"}
    if amount <= 0:
        return {"success": False, "error": "Valid amount required"}

    lc_name = frappe.db.get_value("Loyalty Customer", {"mobile": mobile}, "name")
    if not lc_name:
        return {"success": False, "error": "Account not found"}

    lc = frappe.get_doc("Loyalty Customer", lc_name)
    settings = _get_loyalty_settings()

    if amount < settings["min_redeem"]:
        return {"success": False, "error": f"Minimum redeem is LKR {settings['min_redeem']:.0f}"}

    if (lc.cashback_balance or 0) < amount:
        return {"success": False, "error": f"Insufficient balance. Available: LKR {lc.cashback_balance:.0f}"}

    lc.cashback_balance = (lc.cashback_balance or 0) - amount
    lc.total_redeemed = (lc.total_redeemed or 0) + amount
    lc.flags.ignore_permissions = True
    lc.save(ignore_permissions=True)

    txn = frappe.get_doc({
        "doctype": "Cashback Transaction",
        "loyalty_customer": lc_name,
        "type": "Redeem",
        "amount": amount,
        "notes": f"Redeemed LKR {amount:.0f}",
    })
    txn.flags.ignore_permissions = True
    txn.insert()
    frappe.db.commit()

    return {"success": True, "new_balance": lc.cashback_balance}
@frappe.whitelist(allow_guest=True)
def loyalty_find_referrer():
    ref_code = frappe.local.form_dict.get("ref_code", "").strip().upper()
    if not ref_code:
        return {"found": False}
    lc_name = frappe.db.get_value("Loyalty Customer", {"referral_code": ref_code}, "name")
    if not lc_name:
        return {"found": False, "ref_code": ref_code}
    lc = frappe.get_doc("Loyalty Customer", lc_name)
    return {"found": True, "referrer_name": lc.customer_name or lc.mobile, "ref_code": ref_code}
@frappe.whitelist(allow_guest=True)
def get_kitchen_groups():
    groups = frappe.get_all("Kitchen Group",
        fields=["group_name", "display_order", "color"],
        order_by="display_order asc"
    )
    return {"groups": groups}
@frappe.whitelist(allow_guest=True)
def loyalty_get_settings_api():
    return _get_loyalty_settings()
