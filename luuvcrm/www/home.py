from urllib.parse import urlencode

import frappe

from luuvcrm import branch
from luuvcrm.api import menu_groups


no_cache = 1
login_required = False


# Three genuine dishes and prices from the current public menu. The original
# image files no longer resolve, so the landing owns a
# small, consistently photographed set instead of hot-linking broken assets.
KURUNEGALA_SHOWCASE = (
    {
        "code": "LANDING-POT-BIRYANI",
        "name": "Chicken Pot Biryani (Single)",
        "description": "Naan-wrapped biryani with tandoori chicken, egg, raita and hot sauce.",
        "image": "/assets/luuvcrm/landing/chicken-pot-biryani.webp",
        "rate": 1440,
        "group": "Biryani & Naan Combos",
    },
    {
        "code": "LANDING-CHEESE-KOTTU",
        "name": "Vegetable Cheese Kottu (Regular)",
        "description": "Vegetable kottu finished with rich, creamy melted cheese.",
        "image": "/assets/luuvcrm/landing/vegetable-cheese-kottu.webp",
        "rate": 990,
        "group": "Cheese Kottu",
    },
    {
        "code": "LANDING-HYDERABADI-BIRYANI",
        "name": "Hyderabadi Style Chicken Biryani",
        "description": "Aromatic biryani with tandoori chicken, egg, raita and fresh salad.",
        "image": "/assets/luuvcrm/landing/hyderabadi-chicken-biryani.webp",
        "rate": 1490,
        "group": "Indian Cuisine",
    },
)


def _city(restaurant):
    doc = frappe.get_cached_doc("Restaurant", restaurant)
    city = str(doc.get("city") or "").strip()
    if not city and doc.address:
        city = frappe.db.get_value("Address", doc.address, "city") or ""
    if not city and restaurant.casefold() in {"kurunegala", "dambulla"}:
        city = restaurant
    return city


def _address_line(restaurant, city=""):
    address = frappe.db.get_value("Restaurant", restaurant, "address")
    if not address:
        return f"{city or restaurant}, Sri Lanka"
    city, line1 = frappe.db.get_value(
        "Address", address, ["city", "address_line1"]
    ) or ("", "")
    return ", ".join(part for part in (line1, city) if part) or f"{restaurant}, Sri Lanka"


def _with_query(path, **params):
    return path + "?" + urlencode([(key, value) for key, value in params.items() if value])


def _location(restaurant, featured=False, deep_link=None):
    deep_link = deep_link or {}
    doc = frappe.get_cached_doc("Restaurant", restaurant)
    display_name = str(doc.get("restaurant_name") or restaurant).strip()
    city = _city(restaurant)
    return frappe._dict(
        name=restaurant,
        display_name=display_name,
        city=city,
        label=f"{display_name} — {city}" if city else display_name,
        address=_address_line(restaurant, city),
        featured=featured,
        # /cus is the one customer journey that can order, retain a referral,
        # track the meal and show rewards.  Do not split landing traffic back
        # onto the older /order-only screen.
        order_url=_with_query("/cus", r=restaurant, **deep_link),
        menu_url=_with_query("/menu", r=restaurant, **deep_link),
        rewards_url=_with_query("/cus", r=restaurant, **deep_link, tab="rewards"),
    )


def get_context(context):
    context.no_sidebar = True
    context.no_header = True
    context.no_footer = True

    # Keep the normal public-branch cookie behaviour when somebody reaches the
    # landing page through a branch link. Unlike the ordering pages, however,
    # the landing page never turns into a chooser: it is the place where both
    # locations are introduced and chosen.
    branch.for_page(context)

    restaurants = branch.all_restaurants()
    primary = next(
        (name for name in restaurants if _city(name).casefold() == "kurunegala"),
        restaurants[0] if restaurants else None,
    )
    ordered = ([primary] if primary else []) + [
        name for name in restaurants if name != primary
    ]

    # A visitor may arrive from an older campaign URL at `/` even though new
    # referral QRs intentionally go straight to `/cus`.  Carry only the three
    # supported customer deep-link values into every next-step CTA; branch.for_page
    # also remembers an explicit `?r=` in its short-lived public cookie.
    deep_link = {
        key: str(frappe.form_dict.get(key) or "").strip()
        for key in ("ref", "promo", "phone")
        if str(frappe.form_dict.get(key) or "").strip()
    }
    context.deep_link = frappe._dict(deep_link)
    context.locations = [
        _location(name, featured=(name == primary), deep_link=deep_link)
        for name in ordered
    ]
    context.primary_location = context.locations[0] if context.locations else None
    context.restaurant = frappe.get_doc("Restaurant", primary) if primary else None
    context.menu_groups = menu_groups(primary) if primary else []
    items = [
        item
        for group in context.menu_groups
        for item in group["items"]
        if not item["code"].startswith("_LFT") and item["rate"] > 0
    ]
    # A food dish leads the visual even when Drinks happens to be the first
    # alphabetical menu group. The rest keep the live menu's own order.
    hero = next(
        (item for item in items if item["group"].casefold() not in {"beverage", "beverages"}),
        items[0] if items else None,
    )
    menu_preview = ([hero] if hero else []) + [
        item for item in items if item is not hero
    ][:5]
    context.featured_items = (
        [frappe._dict(item) for item in KURUNEGALA_SHOWCASE]
        if primary and _city(primary).casefold() == "kurunegala"
        else menu_preview
    )
    context.opening_time = "11:00 AM"
    context.closing_time = "11:00 PM"
