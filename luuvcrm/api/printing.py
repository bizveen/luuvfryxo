"""What goes on paper: receipt, guest check, KOT, the print formats, and the print log.

Split out of the single 7,338-line api.py; every name here is re-exported
from `luuvcrm.api`, so the whitelisted method paths are unchanged.
"""

import frappe
from frappe import _

from luuvcrm import activity
from luuvcrm.api.shifts import _get_invoice_doc


# ─── Print Logging ───────────────────────────────────────

@frappe.whitelist(methods=["POST"])
def log_print():
    data = frappe.local.form_dict
    order_name = data.get("order_name", "")
    print_type = data.get("print_type", "")  # "receipt", "kot"
    user = frappe.session.user
    user_full = frappe.get_value("User", user, "full_name") or user

    log = frappe.get_doc({
        "doctype": "Comment",
        "comment_type": "Info",
        "reference_doctype": "POS Order",
        "reference_name": order_name,
        "content": f"{print_type.upper()} printed by {user_full} ({user})",
    })
    log.flags.ignore_permissions = True
    log.insert(ignore_permissions=True)

    # Also log to Frappe error log for audit
    frappe.log_error(
        f"PRINT: {print_type.upper()} | Order: {order_name} | By: {user_full} ({user})",
        "POS Print Log"
    )

    # The action is exactly "print" because activity.reprint_flag() counts rows
    # with that action for this order and this print type. A reprint is normal
    # once; a habit of reprinting the guest check is how a bill gets swapped.
    flag, note = activity.reprint_flag(order_name, print_type)
    total = frappe.db.get_value("POS Order", order_name, "grand_total") if order_name else None
    activity.log_activity(
        "print", label="Printed %s" % (print_type or "document"),
        ref_doctype="POS Order", ref_name=order_name or None,
        item=print_type or None, amount=float(total) if total is not None else None,
        flag_reason=flag, flag_note=note,
        detail={"print_type": print_type})

    return {"status": "logged", "print_type": print_type, "user": user_full}
# ─── Print Data ─────────────────────────────────────────

BEVERAGE_GROUP_WORDS = ("beverage", "drink", "juice", "shake", "tea", "coffee",
                        "mojito", "smoothie", "bar")
BEVERAGE_NAME_WORDS = ("juice", "cola", "coke", "sprite", "soda", "shake", "tea",
                       "coffee", "cappuccino", "latte", "mojito", "beer", "smoothie",
                       "milo", "lassi", "faluda", "water", "nestomalt")
def _is_beverage_item(item_code):
    """Beverages print in their own section on the KOT/bill (they come from the
    bar/fridge, not the kitchen line). Item Group decides; the item name is a
    fallback for items filed under a generic group."""
    d = frappe.db.get_value("Item", item_code, ["item_group", "item_name"], as_dict=True)
    if not d:
        return False
    grp = (d.item_group or "").lower()
    if any(w in grp for w in BEVERAGE_GROUP_WORDS):
        return True
    nm = (d.item_name or item_code).lower()
    return any(w in nm for w in BEVERAGE_NAME_WORDS)
@frappe.whitelist()
def get_receipt_print_data(order_name):
    """Return data for printing a POS receipt."""
    if not order_name:
        frappe.throw(_("Order name is required"))
    doc = frappe.get_doc("POS Order", order_name)
    items = []
    for i in (doc.get("items") or []):
        items.append({
            "item": i.item,
            "item_name": i.item_name or i.item,
            "qty": i.qty,
            "rate": i.rate,
            "takeaway": int(i.get("takeaway") or 0),
            "beverage": 1 if _is_beverage_item(i.item) else 0,
        })

    invoice_name = doc.pos_invoice or ""
    payment_mode = ""
    if invoice_name:
        inv = _get_invoice_doc(invoice_name)
        if inv.payments:
            payment_mode = inv.payments[0].mode_of_payment

    return {
        "order_name": doc.name,
        "invoice_name": invoice_name,
        "customer_name": doc.customer_name or "Walk-in",
        "table": doc.restaurant_table or "",
        "waiter_name": doc.waiter_name or "",
        "grand_total": doc.grand_total or 0,
        "service_charge_rate": doc.service_charge_rate or 0,
        "service_charge_amount": doc.service_charge_amount or 0,
        "payment_mode": payment_mode,
        "items": items,
        "creation": str(doc.creation),
    }
@frappe.whitelist()
def get_guest_check_data(order_name):
    """Return data for printing a Guest Check (pre-bill)."""
    if not order_name:
        frappe.throw(_("Order name is required"))
    doc = frappe.get_doc("POS Order", order_name)
    items = []
    for i in (doc.get("items") or []):
        items.append({
            "item": i.item,
            "item_name": i.item_name or i.item,
            "qty": i.qty,
            "rate": i.rate,
            "takeaway": int(i.get("takeaway") or 0),
            "beverage": 1 if _is_beverage_item(i.item) else 0,
        })

    return {
        "order_name": doc.name,
        "customer_name": doc.customer_name or "Walk-in",
        "table": doc.restaurant_table or "",
        "waiter_name": doc.waiter_name or "",
        "order_type": doc.order_type or "Dine In",
        "grand_total": doc.grand_total or 0,
        "service_charge_rate": doc.service_charge_rate or 0,
        "service_charge_amount": doc.service_charge_amount or 0,
        "items": items,
        "creation": str(doc.creation),
    }
@frappe.whitelist()
def get_kot_print_data(order_name):
    """Return data for printing a Kitchen Order Ticket."""
    if not order_name:
        frappe.throw(_("Order name is required"))
    doc = frappe.get_doc("POS Order", order_name)
    items = []
    for i in (doc.get("items") or []):
        # Only items not yet fired to the kitchen (a re-print shows only newly added items).
        new_qty = int(i.qty or 0) - int(i.get("sent_qty") or 0)
        if new_qty <= 0:
            continue
        items.append({
            "item": i.item,
            "item_name": i.item_name or i.item,
            "qty": new_qty,
            "takeaway": int(i.get("takeaway") or 0),
            "notes": i.get("notes") or "",
            "beverage": 1 if _is_beverage_item(i.item) else 0,
        })

    return {
        "order_name": doc.name,
        "table": doc.restaurant_table or "",
        "order_type": doc.get("order_type") or "",
        "waiter_name": doc.waiter_name or "",
        "order_source": doc.order_source or "Walk-in",
        "notes": doc.notes or "",
        "items": items,
        "creation": str(doc.creation),
    }
@frappe.whitelist()
def get_pos_print_formats():
    """Print Formats available for POS printing, grouped by target doctype.
    'invoice' formats render the receipt (POS Invoice); 'order' formats render
    the guest check and KOT (POS Order)."""
    def _fmts(dt):
        return frappe.get_all(
            "Print Format",
            filters={"doc_type": dt, "disabled": 0},
            fields=["name"], order_by="name",
        )
    return {
        "invoice": _fmts("POS Invoice"),   # receipt
        "order": _fmts("POS Order"),       # guest check + KOT
    }
@frappe.whitelist()
def render_pos_print(order_name, kind="receipt", print_format=None):
    """Render a POS document with a selected Frappe Print Format.
    kind: 'receipt' -> POS Invoice; 'guest_check'/'kot' -> POS Order.
    Returns {"html": ""} when no format is chosen (or the order is not yet
    invoiced for a receipt), so the caller falls back to the built-in layout."""
    if not order_name:
        frappe.throw(_("Order name is required"))
    if not print_format:
        return {"html": ""}
    if kind == "receipt":
        order = frappe.get_doc("POS Order", order_name)
        if not order.pos_invoice:
            return {"html": ""}   # not paid yet → fall back to built-in
        dt = "POS Invoice" if frappe.db.exists("POS Invoice", order.pos_invoice) else "Sales Invoice"
        dn = order.pos_invoice
    else:
        dt, dn = "POS Order", order_name
    html = frappe.get_print(dt, dn, print_format=print_format)
    return {"html": html}
# ─── Thermal print formats: FOOD / BEVERAGES / TAKEAWAY sections ────────────
# The three DB Print Formats are the PRIMARY print path (render_pos_print);
# The POS's built-in builders are only the fallback. Their HTML lives here so it
# is version-controlled; setup_print_sections() pushes it into the DB docs.
# The Jinja classifier mirrors _is_beverage_item(): Item Group first, item-name
# keywords as fallback. Section headers print only when >1 section has items.

_PF_BEV_JINJA = '''{%- set BG = ["beverage","drink","juice","shake","tea","coffee","mojito","smoothie","bar"] -%}
{%- set BN = ["juice","cola","coke","sprite","soda","shake","tea","coffee","cappuccino","latte","mojito","beer","smoothie","milo","lassi","faluda","water","nestomalt"] -%}'''
KOT_PF_HTML = '''<style>
@page { size: 80mm auto; margin: 0; }
* { box-sizing: border-box; }
body { font-family: Arial, Helvetica, sans-serif; font-size: 11px; color: #000; background: #fff; margin: 0; padding: 0; }
.print-format { width: 100%; max-width: 80mm; margin: 0; padding: 1mm 1.5mm 3mm; }
.type-banner { text-align: center; font-size: 14px; font-weight: 900; color: #000; border: 2px solid #000; border-radius: 3px; padding: 5px 4px; margin: 0 0 5px; word-break: break-word; overflow-wrap: break-word; }
.dv { border-top: 1px dashed #000; margin: 4px 0; }
table.it { width: 100%; table-layout: fixed; border-collapse: collapse; margin: 3px 0; }
table.it th { font-size: 12px; font-weight: 800; text-transform: uppercase; letter-spacing: 1px; color: #000; border-bottom: 2px solid #000; padding: 4px 0; }
table.it th:nth-child(1) { text-align: left; width: 78%; }
table.it th:nth-child(2) { text-align: center; width: 22%; }
table.it td { padding: 4px 0; font-size: 14px; font-weight: 700; vertical-align: top; color: #000; word-break: break-word; overflow-wrap: break-word; }
table.it td:nth-child(1) { text-align: left; }
table.it td:nth-child(2) { text-align: center; font-weight: 800; }
table.it td.sect { text-align: center; font-weight: 900; font-size: 11px; letter-spacing: 1.5px; padding: 5px 0 2px; border-top: 1px dashed #000; }
.nt { font-size: 11px; font-weight: 600; font-style: italic; }
.fm { text-align: center; margin-top: 5px; font-size: 11px; color: #000; font-weight: 700; text-transform: uppercase; }
</style>
<div class="type-banner">
{%- set ot = (doc.order_type or "Dine In") -%}
{%- if ot == "Take Away" -%}Take Away
{%- elif ot == "Delivery" -%}Delivery
{%- else -%}Dine in{%- if doc.restaurant_table %} - T{{ doc.restaurant_table.split('-')[-1] }}{%- endif -%}
{%- endif -%}, {{ frappe.utils.format_datetime(doc.creation, "dd,MMM, hh.mma") }}
</div>
''' + _PF_BEV_JINJA + '''
{%- set S = namespace(food=[], bev=[], ta=[], n=0) -%}
{%- for item in doc.items -%}
  {%- set nq = (item.qty or 0) - (item.sent_qty or 0) -%}
  {%- if nq > 0 -%}
    {%- set S.n = S.n + 1 -%}
    {%- if item.takeaway -%}
      {%- set S.ta = S.ta + [(item, nq)] -%}
    {%- else -%}
      {%- set grp = (frappe.db.get_value("Item", item.item, "item_group") or "") | lower -%}
      {%- set nm = (item.item_name or item.item) | lower -%}
      {%- set B = namespace(v=false) -%}
      {%- for w in BG -%}{%- if w in grp -%}{%- set B.v = true -%}{%- endif -%}{%- endfor -%}
      {%- for w in BN -%}{%- if w in nm -%}{%- set B.v = true -%}{%- endif -%}{%- endfor -%}
      {%- if B.v -%}{%- set S.bev = S.bev + [(item, nq)] -%}{%- else -%}{%- set S.food = S.food + [(item, nq)] -%}{%- endif -%}
    {%- endif -%}
  {%- endif -%}
{%- endfor -%}
{%- set multi = ((1 if S.food else 0) + (1 if S.bev else 0) + (1 if S.ta else 0)) > 1 -%}
<table class="it"><thead><tr><th>Item</th><th>Qty</th></tr></thead>
<tbody>
{%- for sec_title, sec_items in [("FOOD", S.food), ("BEVERAGES", S.bev), ("TAKEAWAY", S.ta)] -%}
  {%- if sec_items -%}
    {%- if multi -%}<tr><td colspan="2" class="sect">&mdash; {{ sec_title }} &mdash;</td></tr>{%- endif -%}
    {%- for item, nq in sec_items -%}
<tr><td>{% if item.takeaway %}(takeaway) {% endif %}{{ item.item_name or item.item }}{% if item.notes %}<div class="nt">&rarr; {{ item.notes }}</div>{% endif %}</td><td>{{ nq }}</td></tr>
    {%- endfor -%}
  {%- endif -%}
{%- endfor -%}
{%- if S.n == 0 -%}
<tr><td colspan="2" style="text-align:center;padding:8px 0;font-weight:600;">No new items</td></tr>
{%- endif -%}
</tbody></table>
<div class="dv"></div>
<div class="fm">Kitchen Order Ticket</div>
'''
_PF_BILL_CSS_EXTRA = '''table.it td.sect { text-align: center; font-weight: 900; font-size: 9px; letter-spacing: 1.5px; padding: 4px 0 1px; border-top: 1px dashed #000; }
'''
_PF_ORDER_SECTION_JINJA = _PF_BEV_JINJA + '''
{%- set S = namespace(food=[], bev=[], ta=[]) -%}
{%- for item in doc.items -%}
  {%- if item.takeaway -%}
    {%- set S.ta = S.ta + [item] -%}
  {%- else -%}
    {%- set grp = (frappe.db.get_value("Item", item.item, "item_group") or "") | lower -%}
    {%- set nm = (item.item_name or item.item) | lower -%}
    {%- set B = namespace(v=false) -%}
    {%- for w in BG -%}{%- if w in grp -%}{%- set B.v = true -%}{%- endif -%}{%- endfor -%}
    {%- for w in BN -%}{%- if w in nm -%}{%- set B.v = true -%}{%- endif -%}{%- endfor -%}
    {%- if B.v -%}{%- set S.bev = S.bev + [item] -%}{%- else -%}{%- set S.food = S.food + [item] -%}{%- endif -%}
  {%- endif -%}
{%- endfor -%}
{%- set multi = ((1 if S.food else 0) + (1 if S.bev else 0) + (1 if S.ta else 0)) > 1 -%}'''
GUEST_CHECK_PF_HTML = '''<style>
@page { size: 80mm auto; margin: 0; }
* { box-sizing: border-box; }
body { font-family: Arial, Helvetica, sans-serif; font-size: 9px; color: #000; background: #fff; margin: 0; padding: 0; }
.print-format { width: 100%; max-width: 80mm; margin: 0; padding: 1mm 1mm 2mm; }
.r-header { text-align: center; margin-bottom: 3px; }
.r-header h1 { font-size: 14px; font-weight: 900; letter-spacing: 2px; text-transform: uppercase; margin: 0; color: #000; }
.r-header h2 { font-size: 10px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase; margin: 1px 0 2px; color: #000; }
.r-header .sub { font-size: 8px; color: #000; font-weight: 600; letter-spacing: 1px; text-transform: uppercase; }
.sec { border-top: 1px solid #000; margin: 4px 0; }
.i-row { display: flex; justify-content: space-between; font-size: 9px; padding: 1px 0; }
.i-row .lb { color: #000; font-weight: 600; }
.i-row .vl { font-weight: 700; text-align: right; color: #000; }
table.it { width: 100%; table-layout: fixed; border-collapse: collapse; margin: 2px 0; }
table.it th { font-size: 9px; font-weight: 800; text-transform: uppercase; letter-spacing: 1px; color: #000; border-bottom: 1px solid #000; padding: 3px 0; }
table.it th:nth-child(1) { text-align: left; width: 64%; }
table.it th:nth-child(2) { text-align: right; width: 36%; }
table.it td { padding: 2px 0; font-size: 10px; font-weight: 700; vertical-align: top; color: #000; }
table.it td:nth-child(1) { text-align: left; word-break: break-word; overflow-wrap: break-word; }
/* Amount must never wrap: table-layout is fixed, so a break here would split "Rs.24,500" across two lines. */
table.it td:nth-child(2) { text-align: right; font-weight: 800; white-space: nowrap; }
''' + _PF_BILL_CSS_EXTRA + '''table.tt { width: 100%; border-collapse: collapse; margin: 2px 0; }
table.tt td { padding: 1px 0; font-size: 10px; font-weight: 700; color: #000; }
table.tt td:nth-child(1) { text-align: left; width: 62%; }
table.tt td:nth-child(2) { text-align: right; width: 38%; white-space: nowrap; }
table.tt .gt td { font-size: 12px; font-weight: 900; padding: 3px 0; border-top: 1px solid #000; }
.fm { text-align: center; margin-top: 4px; font-size: 8px; color: #000; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; }
.note { text-align: center; font-size: 7px; font-weight: 600; margin-top: 2px; }
</style>
<div class="r-header"><h1>{{ brand().brand_name }}</h1><h2>Guest Check</h2><div class="sub">{{ brand().outlet_line }}</div></div>
<div class="i-row"><span class="lb">Order</span><span class="vl">{{ doc.name }}</span></div>
<div class="i-row"><span class="lb">Date</span><span class="vl">{{ frappe.utils.format_datetime(doc.creation, "dd MMM, hh:mm a") }}</span></div>
{% if doc.restaurant_table %}<div class="i-row"><span class="lb">Table</span><span class="vl">{{ doc.restaurant_table }}</span></div>{% endif %}
{% if doc.customer_name %}<div class="i-row"><span class="lb">Customer</span><span class="vl">{{ doc.customer_name }}</span></div>{% endif %}
{% if doc.waiter_name %}<div class="i-row"><span class="lb">Server</span><span class="vl">{{ doc.waiter_name }}</span></div>{% endif %}
<div class="sec"></div>
{%- set ns = namespace(sub=0) -%}
{%- for i in doc.items -%}{%- set ns.sub = ns.sub + (i.qty * i.rate) -%}{%- endfor -%}
''' + _PF_ORDER_SECTION_JINJA + '''
<table class="it"><thead><tr><th>Item</th><th>Amount</th></tr></thead>
<tbody>
{%- for sec_title, sec_items in [("FOOD", S.food), ("BEVERAGES", S.bev), ("TAKEAWAY", S.ta)] -%}
  {%- if sec_items -%}
    {%- if multi -%}<tr><td colspan="2" class="sect">&mdash; {{ sec_title }} &mdash;</td></tr>{%- endif -%}
    {%- for item in sec_items -%}
<tr><td>{% if item.takeaway %}(takeaway) {% endif %}{{ item.item_name or item.item }} X{{ item.qty }}</td><td>Rs.{{ frappe.utils.fmt_money(item.qty * item.rate, 0) }}</td></tr>
    {%- endfor -%}
  {%- endif -%}
{%- endfor -%}
</tbody></table>
<div class="sec"></div>
<table class="tt"><tbody>
<tr><td>Subtotal</td><td>Rs.{{ frappe.utils.fmt_money(ns.sub, 0) }}</td></tr>
{%- if doc.get("discount_amount") and doc.discount_amount > 0 -%}
<tr><td>Discount{% if doc.get("discount_note") %} ({{ doc.discount_note }}){% endif %}</td><td>&minus; Rs.{{ frappe.utils.fmt_money(doc.discount_amount, 0) }}</td></tr>
{%- endif -%}
{%- if doc.service_charge_amount and doc.service_charge_amount > 0 -%}
<tr><td>Service charge{% if doc.service_charge_rate %} ({{ doc.service_charge_rate }}%){% endif %}</td><td>Rs.{{ frappe.utils.fmt_money(doc.service_charge_amount, 0) }}</td></tr>
{%- endif -%}
<tr class="gt"><td>Total</td><td>Rs.{{ frappe.utils.fmt_money(doc.grand_total, 0) }}</td></tr>
</tbody></table>
{%- if S.ta -%}<div class="note">Take-away items carry no service charge.</div>{%- endif -%}
<div class="sec"></div>
<div class="fm">Guest check &mdash; not a final bill</div>
'''
RECEIPT_PF_HTML = '''<style>
@page { size: 80mm auto; margin: 0; }
* { box-sizing: border-box; }
body { font-family: Arial, Helvetica, sans-serif; font-size: 9px; color: #000; background: #fff; margin: 0; padding: 0; }
.print-format { width: 100%; max-width: 80mm; margin: 0; padding: 1mm 1mm 2mm; }
.r-header { text-align: center; margin-bottom: 3px; }
.r-header h1 { font-size: 14px; font-weight: 900; letter-spacing: 2px; text-transform: uppercase; margin: 0; color: #000; }
.r-header h2 { font-size: 10px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase; margin: 1px 0 2px; color: #000; }
.r-header .sub { font-size: 8px; color: #000; font-weight: 600; letter-spacing: 1px; text-transform: uppercase; }
.sec { border-top: 1px solid #000; margin: 4px 0; }
.i-row { display: flex; justify-content: space-between; font-size: 9px; padding: 1px 0; }
.i-row .lb { color: #000; font-weight: 600; }
.i-row .vl { font-weight: 700; text-align: right; color: #000; }
table.it { width: 100%; table-layout: fixed; border-collapse: collapse; margin: 2px 0; }
table.it th { font-size: 9px; font-weight: 800; text-transform: uppercase; letter-spacing: 1px; color: #000; border-bottom: 1px solid #000; padding: 3px 0; }
table.it th:nth-child(1) { text-align: left; width: 64%; }
table.it th:nth-child(2) { text-align: right; width: 36%; }
table.it td { padding: 2px 0; font-size: 10px; font-weight: 700; vertical-align: top; color: #000; }
table.it td:nth-child(1) { text-align: left; word-break: break-word; overflow-wrap: break-word; }
/* Amount must never wrap: table-layout is fixed, so a break here would split "Rs.24,500" across two lines. */
table.it td:nth-child(2) { text-align: right; font-weight: 800; white-space: nowrap; }
''' + _PF_BILL_CSS_EXTRA + '''table.tt { width: 100%; border-collapse: collapse; margin: 2px 0; }
table.tt td { padding: 1px 0; font-size: 10px; font-weight: 700; color: #000; }
table.tt td:nth-child(1) { text-align: left; width: 62%; }
table.tt td:nth-child(2) { text-align: right; width: 38%; white-space: nowrap; }
table.tt .gt td { font-size: 12px; font-weight: 900; padding: 3px 0; border-top: 1px solid #000; }
.fm { text-align: center; margin-top: 4px; font-size: 8px; color: #000; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; }
.tel { text-align: center; margin-top: 2px; font-size: 11px; font-weight: 900; letter-spacing: 1px; color: #000; }
</style>
<div class="r-header"><h1>{{ brand().brand_name }}</h1><h2>Receipt</h2><div class="sub">{{ brand().outlet_line }}</div></div>
<div class="i-row"><span class="lb">Bill</span><span class="vl">{{ doc.name }}</span></div>
<div class="i-row"><span class="lb">Date</span><span class="vl">{{ frappe.utils.format_datetime(doc.creation, "dd MMM, hh:mm a") }}</span></div>
{% if doc.restaurant_table %}<div class="i-row"><span class="lb">Table</span><span class="vl">{{ doc.restaurant_table }}</span></div>{% endif %}
<div class="i-row"><span class="lb">Customer</span><span class="vl">{{ doc.customer_name or "Walk-in" }}</span></div>
<div class="sec"></div>
''' + _PF_BEV_JINJA + '''
{%- set S = namespace(food=[], bev=[], ta=[]) -%}
{%- for item in doc.items -%}
  {%- if item.get("takeaway") -%}
    {%- set S.ta = S.ta + [item] -%}
  {%- else -%}
    {%- set grp = (frappe.db.get_value("Item", item.item_code, "item_group") or "") | lower -%}
    {%- set nm = (item.item_name or item.item_code) | lower -%}
    {%- set B = namespace(v=false) -%}
    {%- for w in BG -%}{%- if w in grp -%}{%- set B.v = true -%}{%- endif -%}{%- endfor -%}
    {%- for w in BN -%}{%- if w in nm -%}{%- set B.v = true -%}{%- endif -%}{%- endfor -%}
    {%- if B.v -%}{%- set S.bev = S.bev + [item] -%}{%- else -%}{%- set S.food = S.food + [item] -%}{%- endif -%}
  {%- endif -%}
{%- endfor -%}
{%- set multi = ((1 if S.food else 0) + (1 if S.bev else 0) + (1 if S.ta else 0)) > 1 -%}
<table class="it"><thead><tr><th>Item</th><th>Amount</th></tr></thead>
<tbody>
{%- for sec_title, sec_items in [("FOOD", S.food), ("BEVERAGES", S.bev), ("TAKEAWAY", S.ta)] -%}
  {%- if sec_items -%}
    {%- if multi -%}<tr><td colspan="2" class="sect">&mdash; {{ sec_title }} &mdash;</td></tr>{%- endif -%}
    {%- for item in sec_items -%}
<tr><td>{% if item.get("takeaway") %}(takeaway) {% endif %}{{ item.item_name or item.item_code }} X{{ item.qty|int }}</td><td>Rs.{{ frappe.utils.fmt_money(item.amount, 0) }}</td></tr>
    {%- endfor -%}
  {%- endif -%}
{%- endfor -%}
</tbody></table>
<div class="sec"></div>
<table class="tt"><tbody>
<tr><td>Subtotal</td><td>Rs.{{ frappe.utils.fmt_money(doc.total, 0) }}</td></tr>
{%- for t in doc.taxes -%}
<tr><td>{{ t.description }}</td><td>Rs.{{ frappe.utils.fmt_money(t.tax_amount, 0) }}</td></tr>
{%- endfor -%}
{%- if doc.discount_amount -%}
<tr><td>Discount</td><td>Rs.{{ frappe.utils.fmt_money(doc.discount_amount, 0) }}</td></tr>
{%- endif -%}
<tr class="gt"><td>Total</td><td>Rs.{{ frappe.utils.fmt_money(doc.grand_total, 0) }}</td></tr>
</tbody></table>
<table class="tt"><tbody>
{%- for p in doc.payments -%}
<tr><td>{{ p.mode_of_payment }}</td><td>Rs.{{ frappe.utils.fmt_money(p.amount, 0) }}</td></tr>
{%- endfor -%}
{%- if doc.get("custom_change_returned") and doc.get("custom_change_returned") > 0 -%}
<tr><td>Tendered</td><td>Rs.{{ frappe.utils.fmt_money(doc.get("custom_tendered_amount") or 0, 0) }}</td></tr>
<tr><td>Balance</td><td>Rs.{{ frappe.utils.fmt_money(doc.get("custom_change_returned"), 0) }}</td></tr>
{%- endif -%}
</tbody></table>
<div class="sec"></div>
<div class="fm">Thank you for your visit!</div>
<div class="tel">{{ brand().contact_phone }}</div>
'''
@frappe.whitelist()
def setup_print_sections():
    """Push the section-aware thermal Print Formats into the DB docs (the
    primary print path). Idempotent — overwrites html on each run.
    Run: bench --site <site> execute luuvcrm.api.setup_print_sections"""
    frappe.only_for("System Manager")
    updated, missing = [], []
    # The names are Print Format records the tills already point at (pos.html's
    # PF_DEFAULTS), so they are identifiers, not branding — renaming one here
    # without renaming the record drops that till back to the built-in layout.
    for name, html in (("Luuv Fryxo KOT", KOT_PF_HTML),
                       ("Guest Check", GUEST_CHECK_PF_HTML),
                       ("Luuv Fryxo Receipt", RECEIPT_PF_HTML)):
        if frappe.db.exists("Print Format", name):
            frappe.db.set_value("Print Format", name, "html", html)
            updated.append(name)
        else:
            missing.append(name)
    frappe.db.commit()
    return {"updated": updated, "missing": missing}
