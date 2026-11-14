"""POS phone attribution, protected purchase history, and app invitations."""
from functools import partial

import frappe
from frappe import _
from frappe.utils import now_datetime

from luuvcrm import branch, roles, settings
from luuvcrm.customer_phone import normalize_mobile, phone_variants

CONTACT = "Customer App Contact"
INVITE_FIELD = "customer_app_invite"


def _pos_branch():
    roles.require_pos()
    restaurant = branch.viewing()
    if not restaurant or restaurant not in branch.available_restaurants():
        frappe.throw(_("Choose an accessible restaurant."), frappe.PermissionError)
    return restaurant


def _mobile(value, optional=False):
    if optional and not str(value or "").strip():
        return ""
    mobile = normalize_mobile(value)
    if not mobile:
        frappe.throw(_("Enter a valid Sri Lankan mobile number, for example 0771234567."))
    return mobile


def _contact(mobile, lock=False):
    if not frappe.db.exists("DocType", CONTACT):
        frappe.throw(_("Customer app setup is not installed."))
    if lock:
        # Concurrent purchases of one phone must share a durable row and lock.
        now = now_datetime()
        frappe.db.sql("""INSERT IGNORE INTO `tabCustomer App Contact`
            (name, creation, modified, owner, modified_by, docstatus, idx, mobile, app_installed)
            VALUES (%s,%s,%s,%s,%s,0,0,%s,0)""",
            (mobile, now, now, frappe.session.user, frappe.session.user, mobile))
        return frappe.get_doc(CONTACT, mobile, for_update=True)
    return frappe.db.get_value(CONTACT, mobile,
        ["app_installed", "app_platform", "invite_status", "invite_order"], as_dict=True) or frappe._dict()


def _known_installed(mobile, contact=None):
    contact = contact if contact is not None else _contact(mobile)
    if contact.get("app_installed"):
        return True
    # Disabled tokens can mean logout, not uninstall. A browser Web token does
    # not prove that the PWA is installed, so only native evidence counts here.
    return bool(frappe.db.exists("DocType", "Customer Push Token") and
        frappe.db.exists("Customer Push Token", {
            "mobile": ["in", phone_variants(mobile)], "platform": ["in", ["Android", "iOS"]],
        }))


def _mark_installed(mobile, platform):
    contact = _contact(mobile, lock=True)
    values = {"app_installed": 1, "app_platform": platform, "app_last_seen": now_datetime()}
    if contact.get("invite_status") == "Queued":
        values.update(invite_status="Skipped", invite_note="App installation recorded before SMS dispatch")
    frappe.db.set_value(CONTACT, mobile, values)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def customer_record_app_install(customer_token=None, platform=None):
    from luuvcrm.api.customers import _mobile_from_token

    data = frappe.local.form_dict
    token = customer_token if customer_token is not None else data.get("customer_token")
    mobile = normalize_mobile(_mobile_from_token(str(token or "").strip()))
    if not mobile:
        return {"success": False, "error": "Login required"}
    platform = str(platform if platform is not None else data.get("platform") or "")
    if platform not in {"Android", "iOS", "PWA"}:
        return {"success": False, "error": "Invalid app platform"}
    _mark_installed(mobile, platform)
    return {"success": True, "app_installed": True}


def prepare_pos_customer(data):
    """Explicit POS-only payload; unrelated waiter/online payloads stay intact."""
    if str(data.get("pos_customer") or "") != "1":
        return {}
    _pos_branch()
    mobile = _mobile(data.get("mobile"), optional=True)
    action = str(data.get("customer_app_action") or "unknown")
    if action not in {"unknown", "installed", "invite"}:
        frappe.throw(_("Choose a valid customer app status."))
    if action != "unknown" and not mobile:
        frappe.throw(_("Enter the customer's phone number first."))
    if not frappe.db.has_column("POS Order", INVITE_FIELD):
        frappe.throw(_("Customer app setup is not installed."))
    if action == "installed":
        _mark_installed(mobile, "Staff")
    installed = _known_installed(mobile) if mobile else False
    return {"mobile": mobile, INVITE_FIELD: int(bool(mobile) and action == "invite" and not installed)}


def _app_action(mobile, invite=False):
    return "installed" if mobile and _known_installed(mobile) else "invite" if invite else "unknown"


@frappe.whitelist(methods=["POST"])
def pos_set_order_customer():
    restaurant = _pos_branch()
    data = frappe.local.form_dict
    name = str(data.get("order_name") or "").strip()
    order_branch = branch.require_access("POS Order", name)
    if order_branch != restaurant:
        frappe.throw(_("This record belongs to another restaurant."), frappe.PermissionError)
    order = frappe.get_doc("POS Order", name, for_update=True)
    if order.docstatus != 0 or order.get("pos_invoice") or order.kitchen_status == "Cancelled":
        frappe.throw(_("Customer details cannot be changed after payment or cancellation."))
    if any(float(item.get("paid_qty") or 0) for item in order.items):
        frappe.throw(_("Customer details cannot be changed after a split payment."))
    if "mobile" not in data:
        frappe.throw(_("Phone number is required."))
    mobile = _mobile(data.get("mobile"), optional=True)
    locked_identity = (str(order.get("waiter_name") or "").strip().upper() == "CUS"
        or order.get("reward_redemption_request") or order.get("customer_offer")
        or float(order.get("reward_redeem_amount") or 0) > 0
        or float(order.get("offer_discount_amount") or 0) > 0)
    if locked_identity and mobile != normalize_mobile(order.get("mobile")):
        frappe.throw(_("Keep the customer phone attached to this app/reward order."))
    values = prepare_pos_customer({"pos_customer": 1, "mobile": mobile,
        "customer_app_action": data.get("app_action") or "unknown"})
    frappe.db.set_value("POS Order", name, values)
    action = _app_action(mobile, values[INVITE_FIELD])
    return {"success": True, "mobile": mobile, "app_action": action, "app_installed": action == "installed"}


@frappe.whitelist()
def pos_customer_history():
    restaurant = _pos_branch()
    data = frappe.local.form_dict
    mobile = _mobile(data.get("mobile"))
    from luuvcrm.api.orders import lines_by_order
    from luuvcrm.api.pos_inventory import list_filter

    filters = {"mobile": ["in", phone_variants(mobile)], branch.FIELD: restaurant,
        "pos_invoice": ["is", "set"], "docstatus": ["!=", 2],
        "kitchen_status": ["!=", "Cancelled"], **list_filter()}
    # Read the customer's permitted sale references in one query; expand item
    # details for only twenty. A cancelled invoice is not a purchase.
    candidates = frappe.get_list("POS Order", filters=filters,
        fields=["name", "creation", "grand_total", "restaurant", "customer_name", "pos_invoice"],
        order_by="creation desc", limit_page_length=0)
    invoice_names = list({order.pos_invoice for order in candidates})
    paid_invoices = set()
    for offset in range(0, len(invoice_names), 500):
        for doctype in ("POS Invoice", "Sales Invoice"):
            paid_invoices.update(frappe.get_all(doctype,
                filters={"name": ["in", invoice_names[offset:offset + 500]], "docstatus": 1, "is_return": 0},
                pluck="name", limit_page_length=0))
    paid = [order for order in candidates if order.pos_invoice in paid_invoices]
    totals = {"purchase_count": len(paid), "purchase_total": sum(float(order.grand_total or 0) for order in paid)}
    orders = paid[:20]
    lines = lines_by_order([order.name for order in orders])
    purchases = [{"name": order.name, "placed_at": str(order.creation),
        "grand_total": float(order.grand_total or 0), "restaurant": order.restaurant,
        "items": [{"item_name": line.item_name or line.item,
            "qty": line.qty, "rate": line.rate} for line in lines.get(order.name, [])]}
        for order in orders]
    invite = False
    if data.get("order_name"):
        branch.require_access("POS Order", data.order_name)
        order = frappe.get_doc("POS Order", data.order_name)
        if normalize_mobile(order.mobile) == mobile:
            invite = bool(order.get(INVITE_FIELD))
    contact = _contact(mobile)
    action = _app_action(mobile, invite)
    customer_name = next((order.customer_name for order in orders
        if order.customer_name and order.customer_name != "Walk-in"), "")
    return {"success": True, "mobile": mobile, "customer_name": customer_name,
        "app_installed": action == "installed", "app_status": "installed" if action == "installed" else "unknown",
        "app_action": action, "invite_status": contact.get("invite_status") or "",
        "purchases": purchases, "purchase_count": int(totals.get("purchase_count") or 0),
        "purchase_total": float(totals.get("purchase_total") or 0)}


def _paid_order(order):
    if order.docstatus == 2 or order.kitchen_status == "Cancelled" or not order.get("pos_invoice"):
        return False
    from luuvcrm.api.shifts import _get_invoice_doc

    if not (frappe.db.exists("POS Invoice", order.pos_invoice)
            or frappe.db.exists("Sales Invoice", order.pos_invoice)):
        return False
    invoice = _get_invoice_doc(order.pos_invoice)
    if invoice.docstatus != 1 or invoice.get("is_return"):
        return False
    # The legacy direct sale is paid with docstatus=0. A partially paid split
    # does not receive pos_invoice until the last part is paid.
    return True


def queue_install_invite(order_name):
    """Never fail a paid sale for a best-effort app invitation."""
    savepoint = "pos_app_invite"
    try:
        frappe.db.savepoint(savepoint)
        order = frappe.get_doc("POS Order", order_name)
        mobile = normalize_mobile(order.get("mobile"))
        if (not mobile or not order.get(INVITE_FIELD)
                or order.order_source not in ("Walk-in", "Waiter") or not _paid_order(order)):
            return False
        from luuvcrm.api.sms import textlk_configured

        if not textlk_configured():
            return False
        contact = _contact(mobile, lock=True)
        if _known_installed(mobile, contact) or contact.get("invite_status") not in (None, "", "Skipped"):
            return False
        frappe.db.set_value(CONTACT, mobile, {"invite_status": "Queued", "invite_order": order_name,
            "invite_queued_at": now_datetime(), "invite_note": ""})
        frappe.db.after_commit.add(partial(_enqueue_invite, mobile, order_name))
        return True
    except Exception as exc:
        try:
            frappe.db.rollback(save_point=savepoint)
        except Exception:
            pass
        _log_error("App invitation not queued", exc)
        return False


def _log_error(message, exc):
    try:
        frappe.log_error(f"{message}: {type(exc).__name__}", "Customer app invitation")
    except Exception:
        pass


def _enqueue_invite(mobile, order_name):
    try:
        frappe.enqueue("luuvcrm.api.pos_customers.send_install_invite", queue="short", timeout=60,
            mobile=mobile, order_name=order_name, job_id="customer-app-invite-" + mobile + "-" + order_name)
    except Exception as exc:
        try:
            frappe.db.set_value(CONTACT, mobile, {"invite_status": "Failed", "invite_note": "Background queue unavailable"})
            frappe.db.commit()
        except Exception:
            pass
        _log_error("App invitation queue error", exc)


def send_install_invite(mobile, order_name):
    """One durable dispatch claim per phone. No automatic retry after dispatch."""
    mobile = _mobile(mobile)
    order = frappe.get_doc("POS Order", order_name, for_update=True)
    contact = _contact(mobile, lock=True)
    if contact.get("invite_status") != "Queued" or contact.get("invite_order") != order_name:
        return {"sent": False, "reason": "already handled"}
    if (_known_installed(mobile, contact) or normalize_mobile(order.mobile) != mobile
            or not order.get(INVITE_FIELD) or not _paid_order(order)):
        frappe.db.set_value(CONTACT, mobile, {"invite_status": "Skipped", "invite_note": "App installed or purchase no longer eligible"})
        frappe.db.commit()
        return {"sent": False, "reason": "not eligible"}
    url = str(frappe.conf.get("customer_app_install_url") or "").strip()
    if not url:
        url = str(settings.get("public_url") or "https://luuvgrand.com").rstrip("/") + "/cus?install=1"
    if not url.startswith("https://") or any(ch.isspace() for ch in url):
        frappe.db.set_value(CONTACT, mobile, {"invite_status": "Failed", "invite_note": "Invalid app installation URL"})
        frappe.db.commit()
        return {"sent": False, "reason": "invalid URL"}
    frappe.db.set_value(CONTACT, mobile, {"invite_status": "Sending", "invite_note": "Dispatch claimed; no automatic retry"})
    frappe.db.commit()
    from luuvcrm.api.sms import send_textlk_sms

    try:
        accepted = send_textlk_sms("94" + mobile[1:],
            "Thanks for shopping at Luuv Fryxo! View your purchases and install our app: " + url)
    except Exception:
        accepted = False
    values = {"invite_status": "Sent" if accepted else "Failed",
        "invite_note": "SMS provider accepted the invitation" if accepted else "SMS delivery not confirmed; no automatic retry"}
    if accepted:
        values["invite_sent_at"] = now_datetime()
    frappe.db.set_value(CONTACT, mobile, values)
    frappe.db.commit()
    return {"sent": bool(accepted)}
