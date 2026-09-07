"""The acceptance test: does a fresh install actually sell anything?

Phase 3's definition of done, and the only one that counts — `bench new-site`,
`bench install-app luuvcrm`, then a real sale, with **no manual step of ours and
no HTTP `setup_*` call**. Everything else in that phase is a means to this.

It cannot be a `FrappeTestCase`: those run against a site that already exists,
and the whole question here is what a site looks like when nobody has touched
it. `scripts/acceptance.sh` builds the site and drops it again; this module is
what runs inside it.

**It reports rather than stops.** The first run of something like this always
fails, and a traceback at step one hides the four problems behind it — so every
step is caught, and what comes back is the list of things a fresh site still
cannot do. That list is the remaining Phase 3 work, written by the software
instead of guessed at.

Run:  bench --site <fresh-site> execute luuvcrm.tests.fresh_install.execute
"""

import json

import frappe

MENU_ITEM = "Acceptance Burger"
MENU_PRICE = 1000.0


class Step:
    """One thing a fresh site has to be able to do."""

    def __init__(self, name, needed_for):
        self.name = name
        self.needed_for = needed_for
        self.ok = None
        self.detail = ""


def _call(endpoint, **kwargs):
    """Call an endpoint the way a request does — through form_dict."""
    previous = frappe.local.form_dict
    frappe.local.form_dict = frappe._dict(kwargs)
    try:
        return endpoint()
    finally:
        frappe.local.form_dict = previous


def execute(verbose=True):
    """Take a sale through a site nobody has configured, and say where it stops."""
    from luuvcrm import api, setup_doctype

    frappe.set_user("Administrator")
    steps = []

    def step(name, needed_for, fn):
        s = Step(name, needed_for)
        steps.append(s)
        try:
            s.detail = fn() or ""
            s.ok = True
        except Exception as e:
            s.ok = False
            s.detail = f"{type(e).__name__}: {str(e)[:200]}"
        return s

    # ── 1 · the schema arrived on its own ────────────────────────────────────
    def schema():
        drift = setup_doctype.report_drift()
        missing = {k: v for k, v in (drift or {}).items() if v}
        if missing:
            raise AssertionError(f"the install left gaps: {missing}")
        return f"{len(setup_doctype.EXPECTED_DOCTYPES)} DocTypes, no drift"

    step("schema", "everything", schema)

    # ── 2 · and it arrived as files, not as runtime custom doctypes ──────────
    def not_custom():
        custom = [d for d in setup_doctype.EXPECTED_DOCTYPES
                  if frappe.db.get_value("DocType", d, "custom")]
        if custom:
            raise AssertionError(f"created at runtime, so `bench migrate` skips them: {custom}")
        return "every DocType is a file, so a field change has a migration path"

    step("schema is in the repo", "upgrades", not_custom)

    # ── 3 · the roles exist and are usable ──────────────────────────────────
    def roles_ready():
        from luuvcrm import roles as roles_mod
        missing = [r for r in roles_mod.LUUVCRM_ROLES if not frappe.db.exists("Role", r)]
        if missing:
            raise AssertionError(f"missing roles: {missing}")
        disabled = [r for r in roles_mod.LUUVCRM_ROLES
                    if frappe.db.get_value("Role", r, "disabled")]
        if disabled:
            raise AssertionError(f"present but disabled, which reads as absent: {disabled}")
        return f"{len(roles_mod.LUUVCRM_ROLES)} roles"

    step("roles", "every gate in the app", roles_ready)

    # ── 4 · ERPNext itself is set up (the installer's own step, not ours) ────
    def company():
        name = frappe.db.get_value("Company", {}, "name")
        if not name:
            raise AssertionError(
                "no Company — ERPNext's setup wizard has not been run. That is the "
                "installer's own step, not one of ours, but a sale cannot be raised "
                "without it: see scripts/acceptance.sh, which runs it before this.")
        return name

    step("company", "raising an invoice", company)

    # ── 5 · a manager can put something on the menu, through the app ────────
    def menu():
        if frappe.db.exists("Item", MENU_ITEM):
            return f"{MENU_ITEM} already on the menu"
        group = frappe.db.get_value("Item Group", {"name": "Food"}, "name") \
            or frappe.db.get_value("Item Group", {"is_group": 0}, "name")
        if not group:
            raise AssertionError("no Item Group to file a menu item under")
        api.add_menu_item(item_name=MENU_ITEM, rate=MENU_PRICE, group=group)
        return f"{MENU_ITEM} at Rs {MENU_PRICE:,.0f} in {group}"

    step("add a menu item", "having anything to sell", menu)

    # ── 6 · the till can open a shift ───────────────────────────────────────
    def shift():
        open_now = frappe.db.get_value("POS Opening Entry",
                                       {"docstatus": 1, "status": "Open"}, "name")
        if open_now:
            return f"a shift is already open ({open_now})"
        result = _call(api.pos_open_shift, floats=json.dumps({"Cash": 0}))
        return f"opened {result.get('name') or result}"

    step("open a shift", "taking money at all", shift)

    # ── 7 · a sale, end to end, through the endpoints the counter uses ───────
    sold = {}

    def sale():
        order = _call(api.send_to_kitchen,
                      items=json.dumps([{"item": MENU_ITEM, "qty": 2}]),
                      order_type="Take Away")
        name = order["name"] if isinstance(order, dict) else order
        sold["order"] = name
        paid = _call(api.process_payment, order_name=name, payment_mode="Cash")
        sold["invoice"] = paid["invoice_name"]
        inv = frappe.get_doc("POS Invoice", sold["invoice"])
        if inv.docstatus != 1:
            raise AssertionError(f"the invoice was left in draft (docstatus {inv.docstatus})")
        if float(inv.grand_total or 0) != MENU_PRICE * 2:
            raise AssertionError(
                f"billed Rs {inv.grand_total} for two at Rs {MENU_PRICE:,.0f}")
        return f"{name} → {sold['invoice']}, Rs {inv.grand_total:,.0f}, submitted"

    step("sell something", "being a point of sale", sale)

    # ── 8 · and the manager can see it ──────────────────────────────────────
    def dashboard():
        d = api.get_manager_dashboard()
        if d["sales_count"] < 1:
            raise AssertionError("the sale is not on the manager's dashboard")
        if float(d["revenue"]) < MENU_PRICE * 2:
            raise AssertionError(f"revenue reads Rs {d['revenue']}")
        return f"Rs {d['revenue']:,.0f} over {d['sales_count']} sale(s)"

    step("the manager sees it", "the reason the owner bought this", dashboard)

    # ── 9 · the rows the screens cannot work without arrived too ────────────
    def seeded():
        """A DocType with no rows in it is a screen that opens empty. These are
        what `after_install` seeds, and what somebody used to have to call a
        `setup_*` endpoint over HTTP to get."""
        gaps = []
        if not frappe.db.count("Kitchen Group"):
            gaps.append("no Kitchen Group — the kitchen display cannot sort a ticket")
        if not frappe.db.count("Expense Category"):
            gaps.append("no Expense Category — an expense cannot be filed")
        if not frappe.db.exists("Customer", setup_doctype.WALK_IN):
            gaps.append(f"no '{setup_doctype.WALK_IN}' customer — a counter sale has nobody to bill")
        if not frappe.db.exists("DocType", "LuuvCRM Settings"):
            gaps.append("no LuuvCRM Settings — the owner cannot set their own brand")
        if gaps:
            raise AssertionError("; ".join(gaps))
        return "kitchen groups, expense categories, the walk-in customer, settings"

    step("the reference rows", "screens that are not empty", seeded)

    passed = [s for s in steps if s.ok]
    failed = [s for s in steps if not s.ok]

    if verbose:
        print("\n─── fresh install: can it sell? ───")
        for s in steps:
            print(f"  {'PASS' if s.ok else 'FAIL'}  {s.name:24} {s.detail}")
        print(f"\n  {len(passed)}/{len(steps)} steps")
        if failed:
            print("\n  A fresh site cannot yet:")
            for s in failed:
                print(f"    · {s.name} — needed for {s.needed_for}")
            print("\n  Those are the remaining Phase 3 items, in the order they bite.")
        else:
            print("\n  A fresh site installs and sells with no manual step of ours.")

    return {"ok": not failed,
            "passed": [s.name for s in passed],
            "failed": [{"step": s.name, "needed_for": s.needed_for, "detail": s.detail}
                       for s in failed]}
