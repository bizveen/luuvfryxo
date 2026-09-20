"""The rate card: a priced menu, made for one tour or transport company.

The manager types the company's name, a discount, and unticks what does not
belong on this card; out comes a clean page to print or keep as a PDF, with the
company's name at the top. Nothing is saved — the form is the whole of it.

Decisions worth knowing before changing anything:

* **The prices are the till's.** Items and rates come from `menu.menu_items()`,
  the one place that decides what is on the menu and what it costs, so a card
  can never quote a dish the till would not sell, or a price it would not charge.
* **Only the discounted price is shown**, never the menu price it came from.
* **Prices round up to the next Rs 10, but never above the menu price.** Rounding
  up keeps the card from promising more discount than was asked for; the cap
  keeps a 0% card, or an item whose menu price is not a multiple of ten, from
  quoting *more* than the menu — a "special rate" dearer than the menu is not
  one.
* **The service charge is a footnote, not arithmetic.** Prices are before it.
"""

import json
import math

import frappe
from frappe import _
from frappe.sessions import get_csrf_token
from luuvcrm import roles, settings
from luuvcrm.api import menu

MAX_DISCOUNT = 50.0
ROUND_TO = 10
SERVICE_NOTE = "+ 10% Service Charge will be added"


def card_rate(rate, discount):
    """The rate on the card: the menu price less the discount, rounded up to the
    next Rs 10, and never above the menu price."""
    rate = float(rate or 0)
    # Rounded to the paisa first: 1000 * 0.9 is 900.0000000000001 in floating
    # point, and rounding *that* up would quote 910 for a 10% discount.
    cut = round(rate * (1 - float(discount) / 100.0), 2)
    return float(min(math.ceil(cut / ROUND_TO) * ROUND_TO, rate))


def _priced_menu():
    """Menu items that have a price, in menu order. An item with no price cannot
    be quoted, so it is not offered — Rs 0 on a rate card is a mistake."""
    return [e for e in menu.menu_items() if e.get("code") and float(e.get("rate") or 0) > 0]


def _grouped(entries):
    groups = {}
    for e in entries:
        groups.setdefault(e.get("group") or "General", []).append(e)
    return groups


@frappe.whitelist()
def get_ratecard_menu():
    """What the form offers: every priced menu item, grouped, in menu order."""
    roles.require_manager()
    entries = _priced_menu()
    return {"groups": [{"group": g, "items": [{"code": e["code"], "name": e["name"],
                                                "rate": float(e["rate"])} for e in items]}
                       for g, items in _grouped(entries).items()],
            "count": len(entries)}


def _parse(company, discount, items):
    """Validate what the form sent; raise something the manager can read."""
    company = " ".join((company or "").split())
    if not company:
        frappe.throw(_("Enter the company name"))
    if len(company) > 80:
        frappe.throw(_("The company name is too long"))
    try:
        discount = float(discount or 0)
    except (TypeError, ValueError):
        frappe.throw(_("The discount must be a number"))
    if not 0 <= discount <= MAX_DISCOUNT:
        frappe.throw(_("The discount must be between 0 and {0}%").format(int(MAX_DISCOUNT)))
    if isinstance(items, str):
        try:
            items = json.loads(items or "[]")
        except ValueError:
            items = []
    wanted = {str(c) for c in (items or [])}
    chosen = [e for e in _priced_menu() if e["code"] in wanted]
    if not chosen:
        frappe.throw(_("Pick at least one item for the rate card"))
    return company, discount, chosen


def ratecard_html(company, discount, entries, toolbar=False, form=None):
    """The card as one self-contained page.

    `toolbar` adds the Print and Download PDF buttons — for the manager's screen
    only. `form` is the fields to send back for the PDF, which has to be a POST
    because the item list does not fit in a URL.
    """
    esc = frappe.utils.escape_html
    body = []
    for group, items in _grouped(entries).items():
        rows = "".join(
            f'<tr><td>{esc(e["name"])}</td><td class="n">{card_rate(e["rate"], discount):,.0f}</td></tr>'
            for e in items)
        body.append(f'<h3>{esc(group)}</h3><table>{rows}</table>')
    bar = ""
    if toolbar and form:
        hidden = "".join(f'<input type="hidden" name="{esc(k)}" value="{esc(str(v))}">'
                         for k, v in form.items())
        bar = ('<div class="bar no-print"><button onclick="window.print()">Print / Save as PDF</button>'
               f'<form method="POST" action="/api/method/luuvcrm.api.print_ratecard">{hidden}'
               '<input type="hidden" name="format" value="pdf">'
               '<button class="alt" type="submit">Download PDF</button></form></div>')
    brand = f'{esc(settings.get("brand_name") or "")} {esc(settings.get("outlet_line") or "")}'.strip()
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Rate card - {esc(company)}</title><style>
body{{font-family:Arial,Helvetica,sans-serif;color:#1c1917;max-width:640px;margin:0 auto;padding:24px 18px;}}
h1{{font-size:24px;margin:0;text-align:center}} .brand{{text-align:center;color:#57534e;margin:6px 0 22px;font-size:15px}}
h3{{font-size:15px;margin:22px 0 6px;padding-bottom:4px;border-bottom:2px solid #1c1917;text-transform:uppercase;letter-spacing:.04em;page-break-after:avoid}}
table{{width:100%;border-collapse:collapse;font-size:14px}} tr{{page-break-inside:avoid}}
td{{padding:6px 4px;border-bottom:1px solid #e7e5e4}} td.n{{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums;width:90px}}
.foot{{margin-top:26px;font-size:12px;color:#57534e;text-align:center}}
.bar{{display:flex;gap:12px;align-items:center;margin-bottom:16px}} .bar form{{margin:0}}
.bar button{{padding:9px 16px;font-size:14px;font-weight:700;border:0;border-radius:8px;background:#F15207;color:#fff}}
.bar button.alt{{background:#44403c}}
@media print{{.no-print{{display:none}} body{{padding:0}}}}
</style></head><body>{bar}
<h1>Special Rates for {esc(company)}</h1><div class="brand">{brand}</div>
{"".join(body)}
<div class="foot">Prices in Rs · {esc(SERVICE_NOTE)}</div>
</body></html>"""


@frappe.whitelist()
def print_ratecard(company=None, discount=0, items=None, format="html"):
    """The rate card as a page to print (`html`) or a file to keep (`pdf`)."""
    roles.require_manager()
    company, discount, entries = _parse(company, discount, items)
    slug = "".join(ch if ch.isalnum() else "-" for ch in company.lower()).strip("-") or "company"
    name = f"rate-card-{slug}"
    if format == "pdf":
        from frappe.utils.pdf import get_pdf
        try:
            pdf = get_pdf(ratecard_html(company, discount, entries))
        except OSError:
            # wkhtmltopdf is not on every server; the page still prints.
            frappe.throw(_("PDF files are not available on this server. Open the rate card and use Print → Save as PDF."))
        frappe.local.response.update({
            "type": "download", "filename": name + ".pdf", "filecontent": pdf,
            "content_type": "application/pdf", "display_content_as": "attachment"})
        return
    form = {"csrf_token": get_csrf_token(), "company": company,
            "discount": discount, "items": json.dumps([e["code"] for e in entries])}
    frappe.local.response.update({
        "type": "download", "filename": name + ".html",
        "filecontent": ratecard_html(company, discount, entries, toolbar=True, form=form),
        "content_type": "text/html", "display_content_as": "inline"})
