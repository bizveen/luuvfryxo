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
    Key("outlet_line", OWNER, "Dambulla",
        "The line under the name on every printed bill — the town or branch."),
    Key("contact_phone", OWNER, "077 8 927 927",
        "Printed at the foot of the receipt, for a customer who has to call."),

    # ── the look ─────────────────────────────────────────────────────────────
    # A second restaurant is not a second Luuv Fryxo. These are read into every
    # page's context by overrides/branding.py and rendered as CSS custom
    # properties, so one value changes the accent on 24 screens at once.
    Key("brand_color", OWNER, "#FB6C03",
        "The accent: buttons, links, the highlighted tab. A hex colour."),
    Key("brand_color_dark", OWNER, "#E85C00",
        "The pressed/hover shade of the accent. A little darker than it."),
    Key("brand_logo", OWNER, "/assets/luuvcrm/app/icon-192.png",
        "Shown in page headers. Any URL the browser can reach — upload one in "
        "the Desk and paste its /files/ link."),
    Key("brand_icon_192", OWNER, "/assets/luuvcrm/app/icon-192.png",
        "Home-screen icon, 192x192 PNG."),
    Key("brand_icon_512", OWNER, "/assets/luuvcrm/app/icon-512.png",
        "Home-screen icon, 512x512 PNG."),

    # ── the deployment's, set in site_config.json ────────────────────────────
    Key("public_url", OPERATOR, "https://luuvgrand.com",
        "Root of the links that go out in WhatsApp. No trailing slash."),
    Key("textlk_api_token", OPERATOR, "", secret=True,
        about="Bearer token for Text.lk customer verification SMS. Keep it "
              "in site_config.json; never put it in the database or git."),
    Key("textlk_sender_id", OPERATOR, "",
        "Approved Text.lk Sender ID used for customer verification SMS."),
    Key("textlk_allow_demo", OPERATOR, False,
        "1 only on a local/test site to permit TextLKDemo. Never enable it on production."),
    # ── the switch that makes a shared bench safe ────────────────────────────
    # This app can be installed on a site that is not a restaurant — on the
    # live bench it is, because that site's own DocTypes live in a module this
    # app owns. Hooks are bench-wide, so everything that runs on every request
    # (branding, the /app redirect) and every patch asks this first, and stays
    # out of a site that only carries the app. Set it on that site:
    #     bench --site <site> set-config luuvcrm_passive 1
    # A restaurant never sets it; a fresh install is active by default.
    Key("passive", OPERATOR, False,
        "1 on a site that has the app installed but is not a restaurant. "
        "Turns off the request hooks and the patches there."),

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


def passive():
    """True on a site that carries the app without being a restaurant.

    Read from site_config only — never from the database — because it is asked
    on every request of every site on the bench, and because the sites it is
    for do not have this app's tables. Never raises, like `get()`.
    """
    try:
        return bool(get("passive"))
    except Exception:
        return False


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
