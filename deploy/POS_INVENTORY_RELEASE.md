# Separate inventory POS — 2026-11-11

`/pos` keeps the current counter layout and creates ordinary orders. Ordinary
orders never check or deduct ingredient stock, including at KOT acknowledgement,
full payment and split payment. Existing orders default to this behavior.

`/pos-inventory` shares the counter template and creates orders with the permanent
`POS Order.luuv_inventory_pos` flag. Missing recipes block kitchen release/payment;
configured recipes use the branch/item stock rules in Manager 2.0 (currently
Block for Kurunegala). Explicit percentage/negative allowances therefore apply
only to inventory orders. Recipes and opening balances are not activated by this
release. Purchase receipts and manager stock counts keep working independently.

The inventory flag is stored at creation, including autosaved drafts, and cannot
be changed by editing an order. Amendments preserve it. Separate POS lists and
draft retry matching avoid mixing the two channels; the kitchen/waiter views
remain shared. Normal inventory KOT printing checks/issues stock before printing,
and subsequent KOT/payment retries use the existing per-line consumed quantity.

The additive patch installs the mode field with default zero; the same definition
is in the fresh-install schema. No historic stock movements are rewritten.

Validation used real ERPNext documents inside rollback-only transactions:

- Ordinary POS with zero stock and missing recipes: KOT, existing-order payment
  and direct payment succeed, with no ingredient stock entry.
- Inventory POS: missing recipes/zero stock reject direct orders, KOT, full and
  split payment; the draft stays unfired and unpaid.
- Draft retries and POS lists respect the mode; waiter lists and amendments
  preserve shared service and mode history. Mode changes are rejected.
- A 150 g recipe deducts exactly once through KOT retry/payment, draft editing
  and direct payment. All sample orders, invoices, items and ledger rows roll back.
- Both native pages render, inline JavaScript passes `node --check`, and login
  redirects return to the corresponding route.

Release tools: `pos_inventory_release.py` captures the live baseline, stages the
code, and uploads one atomic MD5-verified file per SSH command with a precondition
on the original server hash. The database backup precedes the schema change.
`pos_inventory_native_check.py` tests staged code without committing fixtures;
`check_pos_inventory_render.py` verifies staged or deployed rendering. Existing
manager/purchases pages and `pos2.html`/`pos2.py` are protected by checksum checks.
