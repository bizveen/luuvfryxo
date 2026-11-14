"""Customer points, referrals, and the cashback balance.

Split out of the single 7,338-line api.py; every name here is re-exported
from `luuvcrm.api`, so the whitelisted method paths are unchanged.
"""

import frappe
import secrets
import random
import string
from frappe.utils import add_to_date, now_datetime

from luuvcrm import branch, roles
from luuvcrm.api.customers import _customer_mobile
from luuvcrm.api.wa import _local_number


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
                {"fieldname": "type", "label": "Type", "fieldtype": "Select", "options": "Earn\nRedeem\nReversal\nReferral Discount", "reqd": 1, "in_list_view": 1},
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
        def number(field, default):
            value = doc.get(field)
            return float(default if value in (None, "") else value)
        return {
            "level_1_percent": number("level_1_percent", 5.0),
            "level_2_percent": number("level_2_percent", 2.0),
            "level_3_percent": number("level_3_percent", 1.0),
            "min_redeem": number("min_redeem", 200),
            "referral_discount_percent": number("referral_discount_percent", 5.0),
        }
    return {"level_1_percent": 5.0, "level_2_percent": 2.0, "level_3_percent": 1.0, "min_redeem": 200, "referral_discount_percent": 5.0}


def _apply_referral_for_mobile(mobile, ref_code):
    """Attach ``mobile`` to ``ref_code`` without trusting a browser-side total.

    The public endpoint below and the online-order endpoint share this helper.
    Returning the existing relationship as a success here makes checkout
    idempotent (a login may already have attached it), while the public endpoint
    keeps its historical "Already linked" response for callers that need to
    distinguish the first write.
    """
    mobile = (mobile or "").strip()
    ref_code = (ref_code or "").strip().upper()
    if not mobile:
        return {"success": False, "error": "Login required"}
    if not ref_code:
        return {"success": False, "error": "Referral code required"}

    referrer = frappe.db.get_value(
        "Loyalty Customer", {"referral_code": ref_code}, "name"
    )
    if not referrer:
        return {"success": False, "error": "Invalid referral code"}

    ref_doc = frappe.get_doc("Loyalty Customer", referrer)
    if ref_doc.mobile == mobile:
        return {"success": False, "error": "Cannot refer yourself"}

    lc_name = _get_loyalty_customer(mobile)
    lc = frappe.get_doc("Loyalty Customer", lc_name)
    if lc.referred_by:
        if lc.referred_by == referrer:
            return {
                "success": True,
                "already_linked": True,
                "loyalty_customer": lc.name,
                "referrer": ref_doc.customer_name or ref_doc.mobile,
                "referral_code": ref_code,
            }
        return {"success": False, "error": "Already linked to a referrer"}

    lc.referred_by = referrer
    lc.flags.ignore_permissions = True
    lc.save(ignore_permissions=True)
    frappe.db.commit()
    return {
        "success": True,
        "already_linked": False,
        "loyalty_customer": lc.name,
        "referrer": ref_doc.customer_name or ref_doc.mobile,
        "referral_code": ref_code,
    }


def _claim_first_order_referral_discount(mobile, ref_code, order_name, subtotal):
    """Reserve and record the invitee's one-time referral discount.

    This runs *after* the POS Order has been inserted but before its transaction
    commits.  Locking the Loyalty Customer serialises simultaneous checkouts for
    one phone number; the second request sees the first order/ledger row and gets
    no second discount.  The browser only supplies the referral code.  Prices,
    eligibility and the amount all come from server-side records.
    """
    mobile = (mobile or "").strip()
    ref_code = (ref_code or "").strip().upper()
    try:
        subtotal = round(float(subtotal or 0), 2)
    except (TypeError, ValueError):
        subtotal = 0
    if not mobile or not ref_code or not order_name or subtotal <= 0:
        return {"applied": False, "amount": 0, "percent": 0}

    referrer = frappe.db.get_value(
        "Loyalty Customer", {"referral_code": ref_code}, "name"
    )
    if not referrer:
        return {"applied": False, "amount": 0, "percent": 0,
                "error": "Invalid referral code"}

    # FOR UPDATE is the single-use guard.  A phone can only have one Loyalty
    # Customer (mobile is unique), so this is a narrow row lock, not a table lock.
    rows = frappe.db.sql(
        """SELECT name, referred_by
             FROM `tabLoyalty Customer`
            WHERE mobile = %s
            LIMIT 1 FOR UPDATE""",
        mobile,
        as_dict=True,
    )
    if not rows or rows[0].referred_by != referrer:
        return {"applied": False, "amount": 0, "percent": 0,
                "error": "Referral is not linked to this customer"}
    lc_name = rows[0].name

    previous_order = frappe.db.exists(
        "POS Order",
        {
            "mobile": mobile,
            "name": ["!=", order_name],
            "docstatus": ["!=", 2],
            "grand_total": [">", 0],
        },
    )
    already_discounted = frappe.db.exists(
        "Cashback Transaction",
        {"loyalty_customer": lc_name, "type": "Referral Discount"},
    )
    if previous_order or already_discounted:
        return {"applied": False, "amount": 0, "percent": 0,
                "error": "First-order discount already used"}

    percent = max(0.0, min(float(
        _get_loyalty_settings().get("referral_discount_percent") or 0
    ), 100.0))
    amount = min(subtotal, round(subtotal * percent / 100, 2))
    if amount <= 0:
        return {"applied": False, "amount": 0, "percent": percent}

    txn = frappe.get_doc({
        "doctype": "Cashback Transaction",
        "loyalty_customer": lc_name,
        "type": "Referral Discount",
        "amount": amount,
        "reference_order": order_name,
        "notes": f"{percent:g}% first-order referral discount ({ref_code})",
    })
    txn.flags.ignore_permissions = True
    txn.insert()
    return {
        "applied": True,
        "amount": amount,
        "percent": percent,
        "referral_code": ref_code,
    }


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
                    duplicate = frappe.db.exists("Cashback Transaction", {
                        "loyalty_customer": ref_lc.name, "type": "Earn",
                        "level": level_idx + 1, "reference_order": order_name,
                    })
                    if not duplicate:
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

    # Get direct referrals (Level 1 downline).  A referral row must show what
    # that person's orders earned *this referrer*, not the invitee's own
    # ``total_earned`` balance.  The latter only contains rewards earned from
    # the invitee's downline, so a direct referral could generate cashback for
    # the current customer while still incorrectly displaying Rs 0 here.
    downline = frappe.get_all("Loyalty Customer", filters={"referred_by": lc_name},
                              fields=["customer_name", "mobile", "total_earned", "creation"],
                              limit_page_length=200)
    earned_by_mobile = {
        row.mobile: float(row.earned_for_referrer or 0)
        for row in frappe.db.sql(
            """
            SELECT orders.mobile, SUM(txn.amount) AS earned_for_referrer
              FROM `tabCashback Transaction` txn
              JOIN `tabPOS Order` orders
                ON orders.name = txn.reference_order
             WHERE txn.loyalty_customer = %s
               AND txn.type = 'Earn'
               AND txn.level = 1
             GROUP BY orders.mobile
            """,
            lc_name,
            as_dict=True,
        )
    }
    downline_data = [
        {
            "name": d.customer_name or d.mobile,
            "mobile": d.mobile,
            "earned_for_referrer": earned_by_mobile.get(d.mobile, 0),
            # Kept as a compatibility alias for older installed clients.
            "total_earned": earned_by_mobile.get(d.mobile, 0),
        }
        for d in downline
    ]

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

    result = _apply_referral_for_mobile(mobile, ref_code)
    # Preserve the endpoint's established one-shot response even though the
    # internal checkout helper is deliberately idempotent.
    if result.get("already_linked"):
        return {"success": False, "error": "Already linked to a referrer"}
    return result
def _redeem_cashback(mobile, amount, notes="", reference_order="", respect_minimum=True):
    """Atomically redeem one customer's balance and append its money trail."""
    lc_name = frappe.db.get_value("Loyalty Customer", {"mobile": mobile}, "name")
    if not lc_name:
        return {"success": False, "error": "Account not found"}
    # Two counter devices (or a customer and counter) must not both spend the
    # same balance.  Lock before re-reading it into the Document below.
    frappe.db.sql(
        "SELECT name FROM `tabLoyalty Customer` WHERE name=%s FOR UPDATE",
        lc_name,
    )
    lc = frappe.get_doc("Loyalty Customer", lc_name, for_update=True)
    settings = _get_loyalty_settings()

    if amount <= 0:
        return {"success": False, "error": "Valid amount required"}
    if respect_minimum and amount < settings["min_redeem"]:
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
        "reference_order": reference_order,
        "notes": notes or f"Redeemed LKR {amount:.0f}",
    })
    txn.flags.ignore_permissions = True
    txn.insert()
    return {"success": True, "new_balance": lc.cashback_balance}


def _expire_reward_requests(mobile=""):
    if not frappe.db.exists("DocType", "Reward Redemption Request"):
        return
    filters = {"status": "Pending", "expires_on": ["<", now_datetime()]}
    if mobile:
        filters["mobile"] = mobile
    for name in frappe.get_all("Reward Redemption Request", filters=filters,
                               pluck="name", limit_page_length=100):
        frappe.db.set_value("Reward Redemption Request", name, "status", "Expired")


def _new_reward_code():
    for _ in range(50):
        code = "".join(random.choices(string.digits, k=6))
        if not frappe.db.exists("Reward Redemption Request", code):
            return code
    return secrets.token_hex(4).upper()


def _request_reward_redemption(mobile, amount):
    lc_name = frappe.db.get_value("Loyalty Customer", {"mobile": mobile}, "name")
    if not lc_name:
        return {"success": False, "error": "Account not found"}
    frappe.db.sql("SELECT name FROM `tabLoyalty Customer` WHERE name=%s FOR UPDATE", lc_name)
    lc = frappe.get_doc("Loyalty Customer", lc_name)
    settings = _get_loyalty_settings()
    if amount < settings["min_redeem"]:
        return {"success": False, "error": f"Minimum redeem is LKR {settings['min_redeem']:.0f}"}
    if float(lc.cashback_balance or 0) < amount:
        return {"success": False, "error": f"Insufficient balance. Available: LKR {lc.cashback_balance:.0f}"}
    _expire_reward_requests(mobile)
    for name in frappe.get_all("Reward Redemption Request",
                               filters={"mobile": mobile, "status": "Pending"},
                               pluck="name", limit_page_length=20):
        frappe.db.set_value("Reward Redemption Request", name, "status", "Cancelled")
    code = _new_reward_code()
    expires_on = add_to_date(now_datetime(), minutes=10)
    frappe.get_doc({
        "doctype": "Reward Redemption Request", "code": code,
        "loyalty_customer": lc_name, "mobile": mobile, "amount": amount,
        "status": "Pending", "expires_on": expires_on,
    }).insert(ignore_permissions=True)
    return {"success": True, "code": code, "amount": amount,
            "expires_on": str(expires_on), "balance": float(lc.cashback_balance or 0)}


@frappe.whitelist(allow_guest=True)
def loyalty_redeem():
    mobile = _customer_mobile()
    try:
        amount = float(frappe.local.form_dict.get("amount", 0))
    except (TypeError, ValueError):
        amount = 0
    if not mobile:
        return {"success": False, "error": "Login required"}
    if amount <= 0:
        return {"success": False, "error": "Valid amount required"}
    return _request_reward_redemption(mobile, amount)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def loyalty_request_redemption():
    return loyalty_redeem()


@frappe.whitelist()
def manager_get_rewards():
    """Manager dashboard: programme totals, customers and redemption trail."""
    roles.require_manager()
    search = str(frappe.local.form_dict.get("search") or "").strip()
    if search:
        like = f"%{search}%"
        customers = frappe.db.sql(
            """
            SELECT customer_name, mobile, cashback_balance,
                   total_earned, total_redeemed
              FROM `tabLoyalty Customer`
             WHERE mobile LIKE %s OR customer_name LIKE %s
             ORDER BY cashback_balance DESC, modified DESC
             LIMIT 30
            """,
            (like, like),
            as_dict=True,
        )
    else:
        customers = frappe.get_all(
            "Loyalty Customer",
            fields=["customer_name", "mobile", "cashback_balance", "total_earned", "total_redeemed"],
            order_by="cashback_balance desc, modified desc",
            limit_page_length=30,
        )
    totals = frappe.db.sql(
        """
        SELECT COUNT(*) AS customers,
               COALESCE(SUM(cashback_balance), 0) AS outstanding,
               COALESCE(SUM(total_earned), 0) AS earned,
               COALESCE(SUM(total_redeemed), 0) AS redeemed
          FROM `tabLoyalty Customer`
        """,
        as_dict=True,
    )[0]
    recent = frappe.db.sql(
        """
        SELECT txn.name, customer.mobile, customer.customer_name,
               txn.amount, txn.notes, txn.creation
          FROM `tabCashback Transaction` txn
          JOIN `tabLoyalty Customer` customer
            ON customer.name = txn.loyalty_customer
         WHERE txn.type = 'Redeem'
         ORDER BY txn.creation DESC
         LIMIT 30
        """,
        as_dict=True,
    )
    _expire_reward_requests()
    pending = frappe.get_all(
        "Reward Redemption Request", filters={"status": "Pending"},
        fields=["code", "mobile", "amount", "expires_on", "creation"],
        order_by="creation desc", limit_page_length=30,
    ) if frappe.db.exists("DocType", "Reward Redemption Request") else []
    open_orders = frappe.get_list(
        "POS Order", filters={branch.FIELD: branch.viewing(),
                              "mobile": ["is", "set"], "docstatus": 0,
                              "pos_invoice": ["is", "not set"]},
        fields=["name", "mobile", "customer_name", "grand_total", "kitchen_status"],
        order_by="creation desc", limit_page_length=100,
    )
    return {
        "success": True,
        "settings": _get_loyalty_settings(),
        "totals": totals,
        "customers": customers,
        "recent_redemptions": recent,
        "pending_requests": pending,
        "open_orders": open_orders,
    }


@frappe.whitelist()
def manager_customer_reward_history():
    """Look up one customer's balance and cashback ledger for counter staff."""
    roles.require_manager()
    digits = "".join(ch for ch in str(frappe.local.form_dict.get("mobile") or "") if ch.isdigit())
    if digits.startswith("94") and len(digits) == 11:
        digits = "0" + digits[2:]
    if len(digits) != 10 or not digits.startswith("0"):
        return {"success": False, "error": "Enter a valid 10-digit mobile number"}
    international = "94" + digits[1:]
    customer = frappe.db.get_value(
        "Loyalty Customer", {"mobile": ["in", [digits, international, "+" + international]]},
        ["name", "customer_name", "mobile", "cashback_balance", "total_earned", "total_redeemed"],
        as_dict=True,
    )
    if not customer:
        return {"success": False, "error": "No rewards account found for this mobile number"}
    history = frappe.get_all(
        "Cashback Transaction", filters={"loyalty_customer": customer.name},
        fields=["name", "type", "amount", "level", "reference_order", "notes", "creation"],
        order_by="creation desc", limit_page_length=100,
    )
    return {"success": True, "customer": customer, "history": history}


@frappe.whitelist(methods=["POST"])
def manager_redeem_reward():
    roles.require_manager()
    code = str(frappe.local.form_dict.get("code") or "").strip()
    order_name = str(frappe.local.form_dict.get("order_name") or "").strip()
    if not code or not order_name:
        return {"success": False, "error": "Customer code and open order are required"}
    if not frappe.db.exists("Reward Redemption Request", code):
        return {"success": False, "error": "Redemption code not found"}
    frappe.db.sql("SELECT code FROM `tabReward Redemption Request` WHERE code=%s FOR UPDATE", code)
    req = frappe.get_doc("Reward Redemption Request", code)
    if req.status != "Pending":
        return {"success": False, "error": f"This code is {req.status.lower()}"}
    if req.expires_on < now_datetime():
        req.db_set("status", "Expired")
        return {"success": False, "error": "This code expired. Ask the customer for a new one"}
    if not frappe.db.exists("POS Order", order_name):
        return {"success": False, "error": "Order not found"}
    branch.require_access("POS Order", order_name)
    order = frappe.get_doc("POS Order", order_name, for_update=True)
    if order.docstatus != 0 or order.pos_invoice:
        return {"success": False, "error": "Choose an unpaid open order"}
    if any(float(item.get("paid_qty") or 0) > 0 for item in order.get("items") or []):
        return {"success": False, "error": "Rewards cannot be applied after a split payment"}
    if order.get("reward_redemption_request") or float(order.get("reward_redeem_amount") or 0) > 0:
        return {"success": False, "error": "This order already has an approved reward"}
    if _local_number(order.mobile or "") != _local_number(req.mobile or ""):
        return {"success": False, "error": "Code and order belong to different customers"}
    amount = float(req.amount or 0)
    if amount > float(order.grand_total or 0):
        return {"success": False, "error": "Reward cannot exceed the order total"}
    note = str(frappe.local.form_dict.get("notes") or "").strip()[:180]
    actor = frappe.get_value("User", frappe.session.user, "full_name") or frappe.session.user
    result = _redeem_cashback(req.mobile, amount,
                              f"Approved by {actor}" + (f" · {note}" if note else ""),
                              reference_order=order.name)
    if not result.get("success"):
        return result
    order.db_set("reward_redeem_amount", amount)
    order.db_set("reward_redemption_request", req.name)
    from luuvcrm.api.pricing import _apply_takeaway_service_charge
    _apply_takeaway_service_charge(order)
    req.db_set("status", "Approved")
    req.db_set("pos_order", order.name)
    req.db_set("redeemed_by", actor)
    return {**result, "amount": amount, "order_name": order.name,
            "order_total": float(order.grand_total or 0)}


def finalize_reward_redemption(order_name, invoice_name):
    name = frappe.db.get_value("Reward Redemption Request",
                               {"pos_order": order_name, "status": "Approved"}, "name")
    if name:
        frappe.db.set_value("Reward Redemption Request", name,
                            {"status": "Redeemed", "pos_invoice": invoice_name})


def reverse_reward_redemption(order_name):
    # Payment also locks the order first. A late cancellation must not restore
    # rewards after payment has completed, even if its initial read was stale.
    if not frappe.db.exists("POS Order", order_name):
        return False
    order = frappe.get_doc("POS Order", order_name, for_update=True)
    if order.get("pos_invoice"):
        return False
    name = frappe.db.get_value("Reward Redemption Request",
                               {"pos_order": order_name, "status": "Approved"}, "name")
    if not name:
        return False
    req = frappe.get_doc("Reward Redemption Request", name, for_update=True)
    # A concurrent cancellation may have restored this request while we waited.
    if req.status != "Approved":
        return False
    if not req.loyalty_customer or not frappe.db.exists(
            "Loyalty Customer", req.loyalty_customer):
        # A restored/test database can retain the audit request after its
        # customer fixture was removed. Cancellation must still be possible;
        # there is no balance left to restore in that case.
        req.db_set("status", "Cancelled")
        return False
    frappe.db.sql("SELECT name FROM `tabLoyalty Customer` WHERE name=%s FOR UPDATE",
                  req.loyalty_customer)
    lc = frappe.get_doc("Loyalty Customer", req.loyalty_customer, for_update=True)
    amount = float(req.amount or 0)
    lc.cashback_balance = float(lc.cashback_balance or 0) + amount
    lc.total_redeemed = max(0, float(lc.total_redeemed or 0) - amount)
    lc.save(ignore_permissions=True)
    frappe.get_doc({"doctype": "Cashback Transaction",
                    "loyalty_customer": lc.name, "type": "Reversal",
                    "amount": amount, "reference_order": order_name,
                    "notes": f"Reward restored after cancellation of {order_name}"}).insert(ignore_permissions=True)
    req.db_set("status", "Cancelled")
    return True


def reverse_order_cashback(order_name):
    rows = frappe.get_all("Cashback Transaction", filters={
        "reference_order": order_name, "type": "Earn"},
        fields=["name", "loyalty_customer", "amount", "level"], limit_page_length=10)
    for row in rows:
        if frappe.db.exists("Cashback Transaction", {
                "reference_order": order_name, "type": "Reversal",
                "loyalty_customer": row.loyalty_customer, "level": row.level}):
            continue
        lc = frappe.get_doc("Loyalty Customer", row.loyalty_customer)
        amount = float(row.amount or 0)
        lc.cashback_balance = max(0, float(lc.cashback_balance or 0) - amount)
        lc.total_earned = max(0, float(lc.total_earned or 0) - amount)
        lc.save(ignore_permissions=True)
        frappe.get_doc({"doctype": "Cashback Transaction",
                        "loyalty_customer": lc.name, "type": "Reversal",
                        "level": row.level, "amount": amount,
                        "reference_order": order_name,
                        "notes": f"Cashback reversed after cancellation of {order_name}"}).insert(ignore_permissions=True)


@frappe.whitelist(methods=["POST"])
def manager_update_loyalty_settings():
    roles.require_manager()
    doc = frappe.get_single("Loyalty Settings")
    bounds = {
        "level_1_percent": (0, 50),
        "level_2_percent": (0, 50),
        "level_3_percent": (0, 50),
        "referral_discount_percent": (0, 100),
        "min_redeem": (0, 1000000),
    }
    for field, (minimum, maximum) in bounds.items():
        try:
            value = float(frappe.local.form_dict.get(field))
        except (TypeError, ValueError):
            frappe.throw(f"Invalid {field.replace('_', ' ')}")
        doc.set(field, max(minimum, min(value, maximum)))
    doc.flags.ignore_permissions = True
    doc.save(ignore_permissions=True)
    return {"success": True, "settings": _get_loyalty_settings()}
@frappe.whitelist(allow_guest=True)
def loyalty_find_referrer():
    ref_code = frappe.local.form_dict.get("ref_code", "").strip().upper()
    if not ref_code:
        return {"found": False}
    lc_name = frappe.db.get_value("Loyalty Customer", {"referral_code": ref_code}, "name")
    if not lc_name:
        return {"found": False, "ref_code": ref_code}
    lc = frappe.get_doc("Loyalty Customer", lc_name)
    return {"found": True, "referrer_name": lc.customer_name or lc.mobile,
            "ref_code": ref_code,
            "discount_percent": _get_loyalty_settings()["referral_discount_percent"]}
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
