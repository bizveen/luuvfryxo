"""Test data builders.

Tests build the rows they need rather than leaning on whatever the site happens
to contain, so a run means the same thing on a developer's laptop, on CI and on
a fresh site. Every builder registers its cleanup with the calling test.

Names are prefixed `_LFT-` so a failed run leaves something obvious behind
rather than something that looks like real data.
"""

import frappe
from frappe.utils import add_days, today

from luuvcrm import branch

PREFIX = "_LFT-"


def _cleanup(case, doctype, name):
    def drop():
        if frappe.db.exists(doctype, name):
            frappe.delete_doc(doctype, name, force=True, ignore_permissions=True,
                              delete_permanently=True)
    case.addCleanup(drop)


def item_group(case, name=PREFIX + "Group"):
    if not frappe.db.exists("Item Group", name):
        parent = frappe.db.get_value("Item Group", {"is_group": 1}, "name")
        frappe.get_doc({"doctype": "Item Group", "item_group_name": name,
                        "parent_item_group": parent, "is_group": 0}).insert(ignore_permissions=True)
        _cleanup(case, "Item Group", name)
    return name


def item(case, code, standard_rate=0):
    """An item with a standard_rate but deliberately NO Item Price.

    standard_rate is written after the insert, on purpose. ERPNext's Item
    controller creates an Item Price from standard_rate on validate, so passing
    it to insert() would give every test item a hidden Standard Selling price —
    which sits at the top of the resolver's priority list and makes the rest of
    the fallback chain untestable.
    """
    code = PREFIX + code
    if frappe.db.exists("Item", code):
        frappe.delete_doc("Item", code, force=True, ignore_permissions=True)
    frappe.get_doc({
        "doctype": "Item",
        "item_code": code,
        "item_name": code,
        "item_group": item_group(case),
        "stock_uom": "Nos",
        "is_stock_item": 0,
    }).insert(ignore_permissions=True)
    _cleanup(case, "Item", code)
    if standard_rate:
        frappe.db.set_value("Item", code, "standard_rate", standard_rate)
    for name in frappe.get_all("Item Price", filters={"item_code": code}, pluck="name"):
        frappe.delete_doc("Item Price", name, force=True, ignore_permissions=True)
    return code


def item_price(case, item_code, rate, price_list="Standard Selling",
               valid_from=None, valid_upto=None, customer=None, selling=1):
    doc = frappe.get_doc({
        "doctype": "Item Price",
        "item_code": item_code,
        "price_list": price_list,
        "price_list_rate": rate,
        "selling": selling,
        "buying": 0 if selling else 1,
        "valid_from": valid_from,
        "valid_upto": valid_upto,
        "customer": customer,
    }).insert(ignore_permissions=True)
    _cleanup(case, "Item Price", doc.name)
    return doc.name


def price_list(case, name):
    name = PREFIX + name
    if not frappe.db.exists("Price List", name):
        frappe.get_doc({"doctype": "Price List", "price_list_name": name,
                        "selling": 1, "enabled": 1,
                        "currency": frappe.db.get_default("currency") or "LKR"
                        }).insert(ignore_permissions=True)
        _cleanup(case, "Price List", name)
    return name


def menu_rate(case, item_code, rate):
    """Put an item on the active Restaurant Menu at `rate`, and drop the row after.

    This is the fallback that made a client-supplied rate necessary: an item can
    be priced here and nowhere else.

    The child row is inserted directly rather than through menu.save(), because
    hospitality's Restaurant Menu controller rebuilds the menu's whole price list
    on every save — `clear_item_price` runs a bare
    `delete from tabItem Price where price_list = %s` — which would wipe prices
    the test (and anything else on the site) depends on.
    """
    restaurant = branch.current()
    menu_name = frappe.db.get_value("Restaurant", restaurant, "active_menu")
    if not menu_name:
        raise RuntimeError("the test site has no active Restaurant Menu")
    row = frappe.get_doc({
        "doctype": "Restaurant Menu Item",
        "parent": menu_name,
        "parenttype": "Restaurant Menu",
        "parentfield": "items",
        "item": item_code,
        "rate": rate,
        # Explicit, because db_insert() applies no docfield default and every
        # availability-respecting screen — menu_items(), and so the till, the
        # customer menu and the reports — reads 0 as "sold out". A fixture that
        # puts an item on the menu invisibly tests nothing.
        "available": 1,
        "idx": 9000,
    })
    row.db_insert()

    def drop():
        frappe.db.delete("Restaurant Menu Item", {"name": row.name})
        forget_menu_cache()

    case.addCleanup(drop)
    forget_menu_cache()
    return row.name


def forget_menu_cache():
    """_menu_row_rates() caches for the life of a request; a test changes the
    menu inside one."""
    frappe.local._lf_menu_rates = None


def service_charge(case, percent):
    """Set the restaurant's service charge for one test and put it back after."""
    restaurant = branch.current()
    previous = frappe.db.get_value("Restaurant", restaurant, "service_charge")
    frappe.db.set_value("Restaurant", restaurant, "service_charge", percent)
    case.addCleanup(frappe.db.set_value, "Restaurant", restaurant, "service_charge", previous)
    return restaurant


def days_ago(n):
    return add_days(today(), -n)


def yesterday():
    return days_ago(1)


def tomorrow():
    return add_days(today(), 1)


def drop_invoice(case, name, doctype="POS Invoice"):
    """Cancel then delete an invoice the test created.

    A submitted POS Invoice has GL entries behind it, so it cannot simply be
    deleted — cancel first, and swallow what a half-built one throws, because a
    cleanup that raises hides the failure the test was reporting.
    """
    def drop():
        if not frappe.db.exists(doctype, name):
            return
        try:
            doc = frappe.get_doc(doctype, name)
            if doc.docstatus == 1:
                doc.flags.ignore_permissions = True
                doc.cancel()
            frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
        except Exception:
            frappe.db.rollback()

    case.addCleanup(drop)


def payment_mode_account(case, mode="Credit Card"):
    """Give a Mode of Payment a default account for the POS Profile's company.

    ERPNext refuses to submit an invoice paid by a mode with no account behind
    it. Cash usually has one on any site; the card modes often do not, which is
    site configuration rather than anything this app controls — so the test
    supplies it and puts it back.
    """
    profile = frappe.db.get_value("POS Profile", {}, ["name", "company"], as_dict=True)
    company = profile.company
    existing = frappe.db.get_value("Mode of Payment Account",
                                   {"parent": mode, "company": company}, "name")
    if existing and frappe.db.get_value("Mode of Payment Account", existing, "default_account"):
        return existing

    account = frappe.db.get_value("Mode of Payment Account",
                                  {"parent": "Cash", "company": company}, "default_account")
    if not account:
        account = frappe.db.get_value("Account",
                                      {"company": company, "account_type": "Cash",
                                       "is_group": 0}, "name")
    if not account:
        case.skipTest(f"no cash account on {company} to lend {mode}")

    doc = frappe.get_doc("Mode of Payment", mode)
    row = doc.append("accounts", {"company": company, "default_account": account})
    doc.save(ignore_permissions=True)
    frappe.db.commit()

    def drop():
        d = frappe.get_doc("Mode of Payment", mode)
        d.accounts = [a for a in d.accounts if a.name != row.name]
        d.save(ignore_permissions=True)
        frappe.db.commit()

    case.addCleanup(drop)
    return row.name


def delivery_payment_mode(case, mode="PickMe"):
    """A payment method that is neither cash nor card — a delivery aggregator.

    Wires it through the real setup helper, so the test covers what a site
    actually runs rather than a hand-built row.

    Removes only what it created. The site may already offer this mode for real
    — a till taking PickMe is the point of it — and a fixture that deleted it
    would take a live payment method away; one that skipped instead would stop
    testing the moment the feature went into use.

    Deliberately does **not** commit. A commit inside a fixture makes everything
    the test goes on to create durable, so the framework's rollback no longer
    takes the test's own orders back out — which is how this site collected
    ~300 draft POS Orders and quietly crossed the kitchen screen's 200 ceiling.
    """
    from luuvcrm import setup_doctype

    pre_existing = bool(frappe.db.exists("Mode of Payment", mode))
    already_offered = {
        p for p in frappe.get_all("POS Profile", pluck="name")
        if frappe.db.exists("POS Payment Method", {"parent": p, "mode_of_payment": mode})
    }
    setup_doctype.add_payment_mode(mode)

    def drop():
        for profile in frappe.get_all("POS Profile", pluck="name"):
            if profile in already_offered:
                continue
            doc = frappe.get_doc("POS Profile", profile)
            kept = [r for r in doc.payments if r.mode_of_payment != mode]
            if len(kept) != len(doc.payments):
                doc.payments = kept
                doc.save(ignore_permissions=True)
        if not pre_existing:
            frappe.delete_doc("Mode of Payment", mode, force=True, ignore_permissions=True)

    case.addCleanup(drop)
    return mode


def guide(case, tag, percent=5.0, balance=0.0):
    """A Tour Guide with a cashback percent and an opening balance.

    Creates the guide doctypes first: they are built at runtime by
    setup_tour_guides, so a site nobody has run it on has none of them.
    """
    import luuvcrm.api as api

    if not frappe.db.exists("DocType", "Tour Guide"):
        api.setup_tour_guides()

    name = PREFIX + tag
    if frappe.db.exists("Tour Guide", name):
        frappe.delete_doc("Tour Guide", name, force=True, ignore_permissions=True)
    doc = frappe.get_doc({
        "doctype": "Tour Guide",
        "guide_name": name,
        "whatsapp": "9477" + str(abs(hash(tag)) % 10_000_000).zfill(7),
        "role": "Guide",
        "status": "Active",
        "cashback_percent": percent,
        "cashback_balance": balance,
        "total_earned": 0,
    })
    doc.flags.ignore_permissions = True
    doc.insert()
    _cleanup(case, "Tour Guide", doc.name)

    def drop_txns():
        for txn in frappe.get_all("Guide Cashback Txn", filters={"guide": doc.name}, pluck="name"):
            frappe.delete_doc("Guide Cashback Txn", txn, force=True, ignore_permissions=True)

    case.addCleanup(drop_txns)
    return doc.name


def guide_balance(name):
    return float(frappe.db.get_value("Tour Guide", name, "cashback_balance") or 0)


def guide_txns(name, kind=None):
    filters = {"guide": name}
    if kind:
        filters["type"] = kind
    return frappe.get_all("Guide Cashback Txn", filters=filters,
                          fields=["type", "amount", "reference_invoice", "bill_total",
                                  "percent_used", "note"],
                          order_by="creation asc")


# ─── Customer accounts, tokens and the OTP cache ────────────────────────────


def customer_doctypes(case=None):
    """Make sure Customer Account / Customer Token exist, without recreating them.

    `setup_customer_accounts()` deletes both DocTypes before rebuilding them, so
    calling it to "ensure" they exist drops every account and token on the site
    — including rows a test three classes away is still holding a token for.
    Only call it when the DocType is genuinely missing.
    """
    import luuvcrm.api as api

    if not (frappe.db.exists("DocType", "Customer Account")
            and frappe.db.exists("DocType", "Customer Token")):
        api.setup_customer_accounts()


def customer_profile_fields():
    """Add Customer Account's address/area columns if the site has not got them.

    customer_update_profile installs them at runtime, so a test asserting on
    them is only doing what the first real save would do anyway.
    """
    import luuvcrm.api as api

    customer_doctypes()
    if not frappe.db.has_column("Customer Account", "address"):
        api._install_customer_profile_fields()


def drop_customer(case, mobile):
    """Clear the account, tokens and OTP cache for a number — now and afterwards.

    Now as well as afterwards because `customer_set_pin` commits, so a row it
    creates outlives the test's rollback; and the cleanup has to commit its own
    delete for the same reason — otherwise the *next* test's rollback puts the
    row straight back and every later registration is told the account already
    exists.
    """
    def drop():
        for name in frappe.get_all("Customer Account", filters={"mobile": mobile}, pluck="name"):
            frappe.delete_doc("Customer Account", name, force=True, ignore_permissions=True)
        for name in frappe.get_all("Customer Token", filters={"mobile": mobile}, pluck="name"):
            frappe.delete_doc("Customer Token", name, force=True, ignore_permissions=True)
        frappe.db.commit()
        forget_customer_cache(mobile)

    drop()
    case.addCleanup(drop)


def customer_account(case, mobile, pin="1234", name=None, hashed=True):
    """A Customer Account with a PIN, hashed the way customer_set_pin stores it.

    `hashed=False` writes the PIN in plaintext, which is what accounts created
    before Phase 1's S6 look like — the shape customer_verify_pin has to keep
    accepting while it upgrades them.
    """
    import luuvcrm.api as api

    customer_doctypes()
    drop_customer(case, mobile)
    doc = frappe.get_doc({
        "doctype": "Customer Account",
        "mobile": mobile,
        "customer_name": name or (PREFIX + "Customer"),
        "pin": api._hash_pin(pin) if hashed else pin,
    })
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    return doc.name


def customer_token(case, mobile, age_days=0):
    """A Customer Token for a number, optionally backdated past its 7-day life."""
    import secrets

    from frappe.utils import add_days, now_datetime

    customer_doctypes()
    token = secrets.token_hex(16)
    doc = frappe.get_doc({
        "doctype": "Customer Token",
        "token": token,
        "mobile": mobile,
        "created": add_days(now_datetime(), -age_days) if age_days else now_datetime(),
    })
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    _cleanup(case, "Customer Token", doc.name)
    return token


def forget_customer_cache(mobile):
    """Clear the OTP and PIN-lockout keys for a number.

    delete_value drops the key from frappe.local.cache as well as from redis,
    which is the point: a plain get_value() miss earlier in the same bench
    process is remembered locally, so without this a later test reads the stale
    None instead of the code the endpoint just wrote.
    """
    cache = frappe.cache()
    for key in (f"wa_otp:{mobile}", f"wa_otp_cool:{mobile}", f"wa_otp_tries:{mobile}",
                f"wa_verified:{mobile}", f"lf_pin_fail:{mobile}"):
        cache.delete_value(key)
