
from random import sample

import frappe
from frappe.utils import add_days, now_datetime

from luuvcrm import branch, settings
from luuvcrm.api import menu_groups

no_cache = 1
login_required = False


def _popular_item_codes(restaurant, eligible_codes):
    """Top five active-menu items sold over the trailing 24 hours.

    Paid POS Orders are the sales authority used by the manager reports too.
    ``eligible_codes`` is already stripped of Add-ons, so sauce/cheese extras
    can never displace a meal or drink in the customer carousel.
    """
    codes = list(dict.fromkeys(code for code in eligible_codes if code))
    if not restaurant or not codes:
        return []
    placeholders = ", ".join(["%s"] * len(codes))
    rows = frappe.db.sql(
        f"""
        SELECT soi.item, SUM(soi.qty) AS sold_qty
        FROM `tabPOS Order Item` soi
        INNER JOIN `tabPOS Order` so ON so.name = soi.parent
        WHERE so.pos_invoice IS NOT NULL AND so.pos_invoice != ''
          AND so.creation >= %s
          AND so.`{branch.FIELD}` = %s
          AND soi.item IN ({placeholders})
        GROUP BY soi.item
        ORDER BY sold_qty DESC, SUM(soi.rate * soi.qty) DESC, soi.item ASC
        LIMIT 5
        """,
        (add_days(now_datetime(), -1), restaurant, *codes),
        as_dict=True,
    )
    return [row.item for row in rows]


def _random_menu_picks(items):
    """Choose up to five non-addon items when there are no recent sales."""
    candidates = [item for item in items if item.get("group") != "Add-ons"]
    return sample(candidates, min(5, len(candidates)))


def redirect_to_cus():
    """301 to /cus, carrying the query string.

    Three old routes still reach customers: /customer (the PWA's address until
    2026-07-12), /myorders and /my_orders. They are links already sent — in
    WhatsApp threads, on printed QR stickers — so they are kept as aliases
    rather than deleted, and each is four lines of redirect over this.

    The query string has to survive: ?phone= comes from a WhatsApp link, ?ref=
    from a referral, ?promo= from a sticker QR, and dropping it turns a
    personalised link into a generic one.
    """
    qs = frappe.request.query_string.decode() if frappe.request and frappe.request.query_string else ""
    frappe.local.flags.redirect_location = "/cus" + (("?" + qs) if qs else "")
    raise frappe.Redirect


def get_context(context):
    """Customer PWA (/cus — moved off /app, which belongs to Frappe Desk;
    previously /customer, shortened to /cus for WhatsApp/QR links).
    Guest-allowed — auth is the phone + 4-digit PIN token scheme
    (customer_* / loyalty_* APIs), not the Frappe session.
    Mirrors order.py for the menu and manager.py for the PWA shell."""
    context.no_sidebar = True
    context.no_header = True
    context.no_footer = True
    context.csrf_token = frappe.sessions.get_csrf_token()

    # Deep-link params: ?ref=<referral_code> (referral), ?phone=<mobile> (WhatsApp link),
    # ?promo=<sticker QR code> (free-meal campaign)
    context.ref_code = (frappe.form_dict.get("ref", "") or "").strip().upper()
    context.prefill_phone = (frappe.form_dict.get("phone", "") or "").strip()
    context.promo_code = (frappe.form_dict.get("promo", "") or "").strip().upper()
    requested_tab = (frappe.form_dict.get("tab", "") or "").strip().lower()
    # Rewards now lives inside Offers. Keep old ?tab=rewards links working,
    # while making the welcome/status dashboard the customer app's front door.
    if requested_tab == "rewards":
        requested_tab = "offers"
    context.initial_tab = requested_tab if requested_tab in {
        "home", "order", "orders", "offers", "account"
    } else "home"

    # A referral QR is deliberately branch-neutral. Always show its two branch
    # cards before ordering, even when this phone has an older branch cookie.
    # Once a card adds ?r=, the explicit choice is respected and remembered.
    form = frappe.form_dict
    force_branch_choice = bool(
        context.ref_code
        and not (form.get("r") or form.get("restaurant") or form.get("table"))
    )
    restaurant_name = branch.for_page(
        context, force_choose=force_branch_choice
    )
    context.restaurant_name = restaurant_name or settings.get("brand_name")
    context.menu_groups = []
    context.hero_item = None
    context.popular_items = []
    context.popular_has_sales = False
    context.popular_sales_count = 0
    context.menu_item_count = 0

    # Build the menu (same logic as the online order page).
    if restaurant_name:
        context.menu_groups = menu_groups(restaurant_name)
        # Food first, optional extras last. menu_groups is alphabetical by
        # design for shared screens, but the customer card should follow the
        # order printed on the Kurunegala menu artwork.
        customer_group_order = {
            "Loaded Fries": 0,
            "Chips": 1,
            "Drinks": 2,
            "Add-ons": 3,
        }
        context.menu_groups.sort(
            key=lambda group: customer_group_order.get(group["name"], 100)
        )
        artwork_item_order = (
            "KG-FRIES-CHICKEN",
            "KG-FRIES-FISH",
            "KG-FRIES-PRAWN",
            "KG-FRIES-BEEF",
            "KG-FRIES-SAUSAGE",
            "KG-FRIES-MIX",
            "KG-CHIPS-FISH",
            "KG-CHIPS-WIENILET",
            "KG-CHIPS-SPICY",
            "KG-THAI-BLUEBERRY",
            "KG-THAI-STRAWBERRY",
            "KG-THAI-MANGO",
            "KG-SMOOTHIE-MANGO",
            "KG-SMOOTHIE-BLUEBERRY",
            "KG-SMOOTHIE-PASSION",
            "KG-SMOOTHIE-STRAWBERRY",
            "KG-ICED-COFFEE",
            "KG-ICED-CHOCOLATE",
            "KG-LEMONADE-BLUEBERRY",
            "KG-LEMONADE-STRAWBERRY",
            "KG-LEMONADE-PASSION",
            "KG-LEMON-MINT",
            "KG-EXTRA-BROWN",
            "KG-EXTRA-WHITE",
            "KG-EXTRA-GOLDEN",
            "KG-EXTRA-SALSA",
            "KG-EXTRA-HOT",
            "KG-EXTRA-CHEESE",
        )
        artwork_position = {
            code: position for position, code in enumerate(artwork_item_order)
        }
        for group in context.menu_groups:
            group["items"].sort(
                key=lambda row: artwork_position.get(row["code"], len(artwork_position))
            )

        visible_items = [
            item
            for group in context.menu_groups
            for item in group["items"]
            if item.get("rate", 0) > 0
        ]
        food_items = [
            item for item in visible_items
            if item.get("group") not in {"Drinks", "Beverages", "Add-ons"}
        ]
        pictured_food = [item for item in food_items if item.get("image")]
        context.hero_item = (pictured_food or food_items or visible_items or [None])[0]
        item_by_code = {item["code"]: item for item in visible_items}
        popular_codes = _popular_item_codes(
            restaurant_name,
            [item["code"] for item in visible_items if item.get("group") != "Add-ons"],
        )
        # Paid sales from the trailing 24 hours define the ranked top picks.
        # A branch with no sales in that window gets five random active-menu
        # picks so the home section remains useful.
        sold_items = [
            item_by_code[code]
            for code in popular_codes
            if code in item_by_code
        ][:5]
        context.popular_has_sales = bool(sold_items)
        context.popular_sales_count = len(sold_items)
        context.popular_items = sold_items or _random_menu_picks(visible_items)
        context.menu_item_count = len(visible_items)
