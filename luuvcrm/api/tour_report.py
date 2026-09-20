"""What each tour or transport company has brought in, and what it is owed.

At the till a bill is booked under a Tour Company (`POS Invoice.tour_company`) and
the guest pays the full menu price. This is the other half: over a range of dates
it adds up those bills, the items they carried, and the company's discount as a
commission, and prints it as a statement.

Decisions worth knowing before changing anything:

* **The lines come from the invoice, not from the POS Order.** A split bill is
  several invoices for one order, each carrying only its own items; the invoice's
  own lines are the one record of what *that* bill was.
* **The commission is worked out the way the rate card is.** Each line's rate is
  cut by the discount and rounded up to the next Rs 10 (`ratecard.card_rate`), so
  the statement agrees with the prices the company was quoted. A flat percent of
  the total would come out a little higher, and the two documents would disagree.
* **The percent is the one on the invoice**, copied when the bill was raised, so
  raising a company's discount does not restate what was sold last month.
* **Amounts are at menu price, before service charge.** The service charge is the
  restaurant's, not the company's; `billed` (the invoice totals) is shown beside
  them so the two can be reconciled to the till.
"""

import frappe
from frappe import _
from luuvcrm import branch, roles, settings
from luuvcrm.api.ratecard import card_rate
from luuvcrm.api.reports import _report_range


def _window(start, end):
    """The dates asked for, or this month so far when none were."""
    from frappe.utils import get_first_day, getdate, today
    if not (start and end):
        start, end = get_first_day(getdate(today())), today()
    return _report_range(30, start, end, cap=366)[:2]


def _blank(name):
    return {"company": name, "bills": 0, "billed": 0.0, "menu": 0.0, "discounted": 0.0,
            "commission": 0.0, "percents": set(), "days": {}, "items": {}}


def build_report(start, end, company=None):
    """{company: block} for the bills booked under a company in the window."""
    if not frappe.db.has_column("POS Invoice", "tour_company"):
        return {}
    cond, params = branch.sql_cost_center("pi")
    if company:
        which, args = " AND pi.tour_company = %s", (start, end, company)
    else:
        which, args = " AND IFNULL(pi.tour_company, '') <> ''", (start, end)
    where = ("WHERE pi.docstatus = 1 AND pi.posting_date BETWEEN %s AND %s" + which + cond)

    invoices = frappe.db.sql(
        "SELECT pi.name, pi.posting_date AS day, pi.tour_company AS company, "
        "pi.tour_discount AS pct, pi.grand_total AS total "
        f"FROM `tabPOS Invoice` pi {where} ORDER BY pi.posting_date, pi.creation",
        args + params, as_dict=True)
    lines = frappe.db.sql(
        "SELECT pii.parent, pii.item_code, COALESCE(NULLIF(pii.item_name, ''), pii.item_code) AS name, "
        "pii.qty, pii.rate "
        f"FROM `tabPOS Invoice Item` pii INNER JOIN `tabPOS Invoice` pi ON pi.name = pii.parent {where}",
        args + params, as_dict=True)

    blocks, by_name = {}, {}
    for inv in invoices:
        b = blocks.setdefault(inv.company, _blank(inv.company))
        pct = float(inv.pct or 0)
        b["bills"] += 1
        b["billed"] += float(inv.total or 0)
        b["percents"].add(pct)
        day = b["days"].setdefault(str(inv.day), {"date": str(inv.day), "bills": 0, "menu": 0.0,
                                                  "discounted": 0.0, "commission": 0.0})
        day["bills"] += 1
        by_name[inv.name] = (b, day, pct)

    for ln in lines:
        b, day, pct = by_name[ln.parent]
        qty, rate = float(ln.qty or 0), float(ln.rate or 0)
        menu = qty * rate
        discounted = qty * card_rate(rate, pct)
        item = b["items"].setdefault((ln.item_code, ln.name), {
            "name": ln.name, "qty": 0.0, "menu": 0.0, "discounted": 0.0, "commission": 0.0})
        for bucket in (b, day, item):
            bucket["menu"] += menu
            bucket["discounted"] += discounted
            bucket["commission"] += menu - discounted
        item["qty"] += qty
    return blocks


def _tidy(b):
    """One block as plain, rounded, sorted data."""
    r2 = lambda v: round(v, 2)  # noqa: E731
    pcts = sorted(b["percents"])
    days = [dict(d, menu=r2(d["menu"]), discounted=r2(d["discounted"]), commission=r2(d["commission"]))
            for _d, d in sorted(b["days"].items())]
    items = sorted((dict(i, qty=int(i["qty"]) if float(i["qty"]).is_integer() else i["qty"],
                         menu=r2(i["menu"]), discounted=r2(i["discounted"]), commission=r2(i["commission"]))
                    for i in b["items"].values()), key=lambda i: -i["menu"])
    return {"company": b["company"], "bills": b["bills"], "billed": r2(b["billed"]),
            "menu": r2(b["menu"]), "discounted": r2(b["discounted"]), "commission": r2(b["commission"]),
            "percents": pcts, "discount_label": _pct_label(pcts), "days": days, "items": items}


def _pct_label(pcts):
    fmt = lambda p: f"{p:g}%"  # noqa: E731
    if not pcts:
        return ""
    return fmt(pcts[0]) if len(pcts) == 1 else f"{fmt(pcts[0])} to {fmt(pcts[-1])}, as at each sale"


def _period(start, end):
    from frappe.utils import formatdate
    return f"{formatdate(start, 'd MMM yyyy')} – {formatdate(end, 'd MMM yyyy')}"


@frappe.whitelist()
def get_tour_report(start=None, end=None, company=None):
    """Every company's bills over the range, or one company's when `company` is given."""
    roles.require_manager()
    start, end = _window(start, end)
    company = " ".join((company or "").split()) or None
    companies = sorted((_tidy(b) for b in build_report(start, end, company).values()),
                       key=lambda c: -c["commission"])
    return {
        "start": str(start), "end": str(end), "period": _period(start, end), "company": company,
        "companies": companies,
        "names": frappe.get_all("Tour Company", order_by="company_name asc", pluck="name",
                                limit_page_length=0) if frappe.db.exists("DocType", "Tour Company") else [],
        "totals": {k: round(sum(c[k] for c in companies), 2)
                   for k in ("bills", "billed", "menu", "discounted", "commission")},
    }


# ─── The statement ───────────────────────────────────────────────────────────

def _m(v):
    return f"{v:,.2f}"


def statement_html(c, start, end, toolbar=False, pdf_url=""):
    """One company's statement as a self-contained page: print it, or hand it to
    wkhtmltopdf. `c` is one block of `get_tour_report`."""
    esc = frappe.utils.escape_html
    brand = f'{esc(settings.get("brand_name") or "")} {esc(settings.get("outlet_line") or "")}'.strip()
    bar = ""
    if toolbar:
        link = f'<a class="alt" href="{esc(pdf_url)}">Download PDF</a>' if pdf_url else ""
        bar = f'<div class="bar no-print"><button onclick="window.print()">Print / Save as PDF</button>{link}</div>'

    days = "".join(
        f'<tr><td>{esc(d["date"])}</td><td class="n">{d["bills"]}</td><td class="n">{_m(d["menu"])}</td>'
        f'<td class="n">{_m(d["discounted"])}</td><td class="n">{_m(d["commission"])}</td></tr>'
        for d in c["days"]) or '<tr><td colspan="5" class="none">No bills were booked under this company in this period.</td></tr>'
    items = "".join(
        f'<tr><td>{esc(i["name"])}</td><td class="n">{i["qty"]:g}</td><td class="n">{_m(i["menu"])}</td>'
        f'<td class="n">{_m(i["discounted"])}</td><td class="n">{_m(i["commission"])}</td></tr>'
        for i in c["items"])
    head = ('<tr><th>{}</th><th class="n">{}</th><th class="n">Sales</th>'
            '<th class="n">Discounted price</th><th class="n">Commission</th></tr>')
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Statement - {esc(c["company"])} - {esc(_period(start, end))}</title><style>
body{{font-family:Arial,Helvetica,sans-serif;color:#1c1917;max-width:760px;margin:0 auto;padding:24px 18px;}}
h1{{font-size:22px;margin:0;text-align:center}} h2{{font-size:18px;margin:6px 0 0;text-align:center}}
.sub{{text-align:center;color:#57534e;margin:4px 0}} .period{{text-align:center;margin:2px 0 20px;font-weight:600}}
.sum{{display:flex;gap:10px;margin-bottom:22px}} .sum div{{flex:1;border:1px solid #d6d3d1;border-radius:8px;padding:10px 12px}}
.sum b{{display:block;font-size:17px;margin-top:2px;font-variant-numeric:tabular-nums}} .sum span{{font-size:11px;color:#57534e;text-transform:uppercase;letter-spacing:.04em}}
h3{{font-size:14px;margin:22px 0 6px;text-transform:uppercase;letter-spacing:.04em}}
table{{width:100%;border-collapse:collapse;font-size:13px}} tr{{page-break-inside:avoid}}
th{{text-align:left;border-bottom:2px solid #1c1917;padding:6px 4px;font-size:12px}} th.n,td.n{{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}}
td{{padding:6px 4px;border-bottom:1px solid #e7e5e4}} td.none{{color:#78716c;text-align:center;padding:16px}}
tr.total td{{font-weight:700;border-top:2px solid #1c1917;border-bottom:0}}
.foot{{margin-top:24px;font-size:11px;color:#57534e;line-height:1.5}}
.bar{{display:flex;gap:12px;align-items:center;margin-bottom:16px}}
.bar button,.bar a.alt{{padding:9px 16px;font-size:14px;font-weight:700;border:0;border-radius:8px;background:#F15207;color:#fff;text-decoration:none}}
.bar a.alt{{background:#44403c}}
@media print{{.no-print{{display:none}} body{{padding:0}}}}
</style></head><body>{bar}
<h1>Commission statement</h1><h2>{esc(c["company"])}</h2>
<div class="sub">{brand}</div><div class="period">{esc(_period(start, end))}</div>
<div class="sum">
<div><span>Bills</span><b>{c["bills"]}</b></div>
<div><span>Sales</span><b>{_m(c["menu"])}</b></div>
<div><span>Discounted price</span><b>{_m(c["discounted"])}</b></div>
<div><span>Commission ({esc(c["discount_label"] or "-")})</span><b>{_m(c["commission"])}</b></div>
</div>
<h3>Day by day</h3><table>{head.format("Date", "Bills")}{days}
<tr class="total"><td>Total</td><td class="n">{c["bills"]}</td><td class="n">{_m(c["menu"])}</td><td class="n">{_m(c["discounted"])}</td><td class="n">{_m(c["commission"])}</td></tr></table>
<h3>Items</h3><table>{head.format("Item", "Qty")}{items}
<tr class="total"><td>Total</td><td class="n"></td><td class="n">{_m(c["menu"])}</td><td class="n">{_m(c["discounted"])}</td><td class="n">{_m(c["commission"])}</td></tr></table>
<div class="foot">Guests paid the full menu price. Sales are at menu price, before service charge.
Discounted price is the menu price less the company's discount ({esc(c["discount_label"] or "-")}), each price rounded up to the next Rs 10 as on the company's rate card.
Commission is the difference. Amounts in Rs.</div>
</body></html>"""


@frappe.whitelist()
def print_tour_statement(company=None, start=None, end=None, format="html"):
    """A company's statement as a page to print (`html`) or a file to keep (`pdf`)."""
    roles.require_manager()
    company = " ".join((company or "").split())
    if not company or not frappe.db.exists("Tour Company", company):
        frappe.throw(_("Pick a company for the statement"), frappe.DoesNotExistError)
    start, end = _window(start, end)
    block = build_report(start, end, company).get(company) or _blank(company)
    c = _tidy(block)
    slug = "".join(ch if ch.isalnum() else "-" for ch in company.lower()).strip("-") or "company"
    file = f"statement-{slug}-{start}-to-{end}"
    if format == "pdf":
        from frappe.utils.pdf import get_pdf
        try:
            pdf = get_pdf(statement_html(c, start, end))
        except OSError:
            # wkhtmltopdf is not on every server; the page still prints.
            frappe.throw(_("PDF files are not available on this server. Open the statement and use Print → Save as PDF."))
        frappe.local.response.update({
            "type": "download", "filename": file + ".pdf", "filecontent": pdf,
            "content_type": "application/pdf", "display_content_as": "attachment"})
        return
    from urllib.parse import urlencode
    pdf_url = "/api/method/luuvcrm.api.print_tour_statement?" + urlencode(
        {"company": company, "start": str(start), "end": str(end), "format": "pdf"})
    frappe.local.response.update({
        "type": "download", "filename": file + ".html",
        "filecontent": statement_html(c, start, end, toolbar=True, pdf_url=pdf_url),
        "content_type": "text/html", "display_content_as": "inline"})
