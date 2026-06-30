# Changelog

## [Unreleased]

### Added
- **Print Receipt** (`pos.html`): "🖨️ Print Receipt" button in order success overlay. Calls `get_receipt_print_data` API and renders a formatted receipt in a new print window.
- **Print KOT** (`waiter.html`): "🖨️ Print KOT" button in order placement success overlay. Calls `get_kot_print_data` API and renders a Kitchen Order Ticket in a new print window.
- **Print KOT** (`kitchen.html`): "🖨️ KOT" button in each order card footer. Re-prints KOT from the kitchen view.
- **Print API endpoints** (`api.py`): `get_receipt_print_data(order_name)` and `get_kot_print_data(order_name)` — server-side data endpoints that return structured print data (items, totals, payment mode / table, waiter, notes).

### Fixed
- **Shift submission bug** (`api.py`): `pos_open_shift` was calling `opening.insert()` without `opening.submit()`, leaving POS Opening Entries in Draft (docstatus=0). Added `opening.submit()` and changed `pos_close_shift` filter from `{"status": "Open"}` to `{"docstatus": 1}`.
- **kctBtn duplication** (`kitchen.html`): Repeated script runs produced duplicate `const kotBtn` declarations and duplicated button in card footer. Deduplicated.
- **Print button load failure** (all www pages): Closing tag pattern `</script></body></html>` (no newlines) was corrected to match actual file formatting with newlines.

### Changed
- `pos.html`, `waiter.html`, `kitchen.html`, `api.py` — all four files updated for print capability.