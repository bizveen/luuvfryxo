"""Everything that changes from one restaurant to the next.

The app currently names one client in 25 places: "Luuv Fryxo" in 21 pages,
`luuvgrand.com` in three WhatsApp messages, a WhatsApp business number, a pickup
town, and four Google Doc ids. None of that can be shipped to a second customer,
and none of it belongs in a commit.

This is the Laravel arrangement, in Frappe's terms:

    Laravel                     here
    ─────────────────────────   ────────────────────────────────────────────
    .env                        sites/<site>/site_config.json  (frappe.conf)
    config/*.php                DEFAULTS below
    a value in the database     LuuvCRM Settings, a Single DocType

`site_config.json` is Frappe's `.env` and needs nothing invented: it already
sits outside the repo, one file per site, and it is where Frappe itself keeps
the database password. A key put there is never in git and never in a database
backup handed to somebody.

**Where a key lives depends on who owns it**, and each key has exactly one home
so that changing it in the wrong place cannot silently do nothing:

    OPERATOR   whoever deploys — API keys, document ids, the domain.
               site_config.json. Not editable from the Desk, on purpose.
    OWNER      the restaurant — brand name, WhatsApp number, pickup outlet.
               LuuvCRM Settings, changed in the Desk without a deploy.

Read one with `get("brand_name")`. Reading is cheap and cached per
request; nothing here should be called in a loop over invoice lines.
"""

import frappe

OPERATOR = "operator"      # site_config.json
OWNER = "owner"            # LuuvCRM Settings single


class Key:
    __slots__ = ("name", "home", "default", "secret", "about")

    def __init__(self, name, home, default, about, secret=False):
        self.name, self.home, self.default = name, home, default
        self.about, self.secret = about, secret


#: Every client-specific value the app reads. Adding one here and using
#: `get()` is the whole contract — there is no second place to register it.
KEYS = [
    # ── the restaurant's own, changed in the Desk ────────────────────────────
    Key("brand_name", OWNER, "Luuv Fryxo",
        "Shown on every screen, receipt and WhatsApp message."),
    Key("brand_short", OWNER, "Luuv",
        "Where the full name will not fit — the browser tab, a narrow header."),
    Key("wa_business_number", OWNER, "94724444901",
        "The WhatsApp number the sticker QRs open, international digits, no +."),
    Key("wa_order_number", OWNER, "94773429923",
        "The number order confirmations are sent from."),
    Key("pickup_outlet", OWNER, "Kurunegala",
        "Free-meal promo orders are collected here, and nowhere else."),

    # ── the deployment's, set in site_config.json ────────────────────────────
    Key("public_url", OPERATOR, "https://luuvgrand.com",
        "Root of the links that go out in WhatsApp. No trailing slash."),
    Key("print_template_docs", OPERATOR, {}, secret=True,
        about="Google Doc ids for the guest check / KOT / receipt / Z-report "
              "layouts, as {kind: id}. The docs are shared 'anyone with the "
              "link', so the ids are a capability — treat them as secret."),
]

_BY_NAME = {k.name: k for k in KEYS}

SETTINGS_DOCTYPE = "LuuvCRM Settings"


def get(name):
    """The value for `name`: its home first, then the default declared above.

    Never raises. A missing Settings DocType, an unreadable site_config, a
    site mid-migration — all of them fall through to the default, because the
    alternative is a till that will not open over a branding string.
    """
    key = _BY_NAME.get(name)
    if key is None:
        raise KeyError(f"unknown config key {name!r} — declare it in luuvcrm/settings.py")

    try:
        if key.home == OPERATOR:
            value = frappe.conf.get(f"luuvcrm_{name}")
        else:
            value = _from_settings(name)
    except Exception:
        value = None

    return key.default if value in (None, "") else value


def _from_settings(name):
    if not frappe.db or not frappe.db.exists("DocType", SETTINGS_DOCTYPE):
        return None
    # A plain attribute on frappe.local, not frappe.local.__dict__: it is a
    # werkzeug proxy and has no __dict__ to reach through. Same pattern the menu
    # rate cache uses.
    cached = getattr(frappe.local, "_luuvcrm_settings", None)
    if cached is None:
        cached = {}
        frappe.local._luuvcrm_settings = cached
    if name not in cached:
        cached[name] = frappe.db.get_single_value(SETTINGS_DOCTYPE, name)
    return cached[name]


def forget():
    """Drop the per-request cache. For tests, and for anything that writes a
    setting and then reads it back in the same request."""
    frappe.local._luuvcrm_settings = None


def url(path=""):
    """An absolute public link — `url("/cus")`.

    These end up in WhatsApp messages that outlive any deploy, which is why the
    root is a setting and not `frappe.utils.get_url()`: the site may be reached
    on an internal hostname that means nothing to a customer.
    """
    return get("public_url").rstrip("/") + ("/" + path.lstrip("/") if path else "")


def describe():
    """What a new deployment has to fill in. Read-only; prints, returns rows.

        bench --site <site> execute luuvcrm.settings.describe
    """
    rows = []
    for key in KEYS:
        current = get(key.name)
        rows.append({
            "key": key.name,
            "home": "site_config.json" if key.home == OPERATOR else SETTINGS_DOCTYPE,
            "using_default": current == key.default,
            "value": "(set)" if key.secret and current != key.default else current,
            "about": key.about,
        })
    width = max(len(r["key"]) for r in rows)
    for r in rows:
        flag = "default" if r["using_default"] else "set    "
        print(f"  {r['key']:<{width}}  {flag}  {r['home']:<18}  {r['value']}")
    return rows
