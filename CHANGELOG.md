# Changelog

## [Unreleased]

### Added
- **Print Receipt** (`pos.html`): "🖨️ Print Receipt" button in order success overlay. Calls `get_receipt_print_data` API and renders a formatted receipt in a new print window.
- **Print KOT** (`waiter.html`): "🖨️ Print KOT" button in order placement success overlay. Calls `get_kot_print_data` API and renders a Kitchen Order Ticket in a new print window.
- **Print KOT** (`kitchen.html`): "🖨️ KOT" button in each order card footer. Re-prints KOT from the kitchen view.
- **Paper width override** (`pos.html`): `localStorage['lf_paper_cols']` sets the printer's column count (58mm roll = 32, 80mm = 48; default 32). Read once into `PAPER_COLS` and used for both the `----` divider and the right-aligned amount padding in `flatten()`, so an 80mm till no longer needs a code change.
- **Print API endpoints** (`api.py`): `get_receipt_print_data(order_name)` and `get_kot_print_data(order_name)` — server-side data endpoints that return structured print data (items, totals, payment mode / table, waiter, notes).

### Fixed
- **Divider printed half-width** (`pos.html`): a `----` line in the Google Docs layout was hardcoded to a 32-character dash string, so it fell short of the paper and printed left-aligned, looking cut off. It is now a semantic line (`divider:true`): the browser renders it as an `<hr>` that fits the page exactly, and the iMin bridge gets a `PAPER_COLS`-wide, centred dash run (the bridge takes text lines only, so dashes are still the best it can do — a true full-width rule needs a native `drawLine()` in the app). `flatten()` strips the `divider` flag before the payload leaves, since the bridge parses `PrintPayload.lines` strictly.
- **Shift submission bug** (`api.py`): `pos_open_shift` was calling `opening.insert()` without `opening.submit()`, leaving POS Opening Entries in Draft (docstatus=0). Added `opening.submit()` and changed `pos_close_shift` filter from `{"status": "Open"}` to `{"docstatus": 1}`.
- **kctBtn duplication** (`kitchen.html`): Repeated script runs produced duplicate `const kotBtn` declarations and duplicated button in card footer. Deduplicated.
- **Print button load failure** (all www pages): Closing tag pattern `</script></body></html>` (no newlines) was corrected to match actual file formatting with newlines.

### Changed
- **`api.py` is a package** — 7,338 lines split into `luuvcrm/api/`, 22 modules, one per domain (POS, orders, payments, kitchen, loyalty, customers, purchases, expenses, approvals, promos, guides, wastage, WhatsApp, printing, reporting…). `__init__.py` re-exports all 261 names, so every `luuvcrm.api.<name>` path stays exactly as it was — 144 whitelisted endpoints, `hooks.py`, the pages, and links already sent. Sending a WhatsApp message goes through one `wa._send_wa_reply` seam instead of four separate bindings.
- **The customer routes** — `/customer`, `/myorders` and `/my_orders` all 301 to `/cus`, as they already did; what changed is that the two stale 530- and 311-line copies of the retired order-history page behind them are gone (they never rendered — the redirect fires first), and all three now share one `redirect_to_cus()` rather than three copies of the query-string handling. The WhatsApp auto-reply links `/cus` directly instead of bouncing customers through `/myorders`.
- **One POS page** — `/pos2` and `/pos3` are deleted and `/pos` serves what used to be pos3, which is where the tablet layer, the iMin printer bridge and the Google-Docs print layouts actually live. The staging print-only guard went with them, and with it a redeclared `guestCheckClick` that shadowed the real one: the guest check autosaves the ticket again instead of answering "Send to kitchen first". `/admin`'s POS tile, `KNOWN_APPS`, the 401/403 redirect and the `get_print_templates` dotted path all follow. **Counter tablets bookmarked on `/pos2` need re-pointing.**
- `pos.html`, `waiter.html`, `kitchen.html`, `api.py` — all four files updated for print capability.
- `pos.html` — `LFTemplate` divider handling (`toLine`, `toHTML`, `flatten`).
