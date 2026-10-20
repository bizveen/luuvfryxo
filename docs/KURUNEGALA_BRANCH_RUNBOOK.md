# Dambulla + Kurunegala branch runbook

This is the hand-off for an AI or operator adding the Kurunegala restaurant to
the live LUUV CRM site. Treat the live database as irreplaceable business data.
Do not turn this document into an unattended script.

## Target design

- Keep **one Frappe site** and **one Company** (`Luuv Fryxo`).
- Use two `Restaurant` records: `Dambulla` and `Kurunegala`.
- Give each restaurant its own leaf `Cost Center`, leaf `Warehouse`, `POS
  Profile`, menu, tables, QR codes, and branch-restricted staff.
- A `Restaurant Owner` has no Restaurant User Permission and can see both
  branches. A manager has a Restaurant User Permission for their branch.
- Do not create a second Company merely to represent Kurunegala. Consolidated
  accounts stay in the shared Company; Cost Centers separate branch P&L and
  Warehouses separate stock.

## Non-negotiable safety rules

1. Discover and print the live site name; never assume it is `test.localhost`.
2. Confirm a restorable database and files backup before any write.
3. Run migrations and verify the branch fields exist before touching data.
4. Backfill every old branchless row to Dambulla **while Dambulla is still the
   only Restaurant**. Never create Kurunegala first.
5. Audit and backfill historical `POS Invoice.cost_center` separately. The app's
   `branch.backfill()` covers the seven LUUV doctypes, not ERPNext invoices.
6. Keep `apply_strict_user_permissions` off during rollout. Consider enabling
   it only after two-branch operation has been clean for a while and
   `luuvcrm.branch.check()` plus `luuvcrm.branch.unbranched()` are clean.
7. Do not restrict the Owner with a Restaurant User Permission. No restriction
   is how the Owner sees both branches.
8. Do not run a live write, rename, bulk update, or deploy merely because this
   file exists. Show the preflight evidence and obtain explicit approval.

## Phase 1: discovery and backup

From the bench directory:

```bash
cat sites/currentsite.txt
bench --site <live-site> list-apps
bench --site <live-site> backup --with-files
git -C apps/luuvcrm rev-parse --short HEAD
git -C apps/luuvcrm status --short --branch
```

Record the backup paths and confirm the files exist. Also record the current
Restaurants, Company, Cost Centers, Warehouses, POS Profiles, open shifts, and
Restaurant User Permissions. Do not expose passwords or `site_config.json`
secrets in chat or logs.

Check the app order shown by `list-apps`: `luuvcrm` must appear after `erpnext`.
Both apps provide `/home` and `/order`, and Frappe lets the later installed app
answer the route. A wrong order makes `/order` run ERPNext's unrelated page and
return 500. Correct the site installation order before testing; do not add a
bench-wide route override because this bench can also host non-restaurant sites.

## Phase 2: schema and Dambulla history

Deploy the intended LUUV commit using the repository's deploy runbook, then:

```bash
bench --site <live-site> migrate
bench --site <live-site> execute luuvcrm.setup_doctype.report_drift
bench --site <live-site> execute luuvcrm.branch.unbranched
```

Before the backfill, confirm there is exactly one Restaurant and that it is the
existing Dambulla operation. Then explicitly target it:

```bash
bench --site <live-site> execute luuvcrm.branch.backfill \
  --kwargs '{"restaurant":"Dambulla"}'
bench --site <live-site> execute luuvcrm.branch.unbranched
```

The second command must return `{}`. If it does not, stop and investigate. Do
not create Kurunegala.

Historical submitted POS Invoices must also be audited. Existing Dambulla
invoices with an empty Cost Center need the Dambulla Cost Center, but only after
a backup and after sampling proves they all predate Kurunegala. There is no
general-purpose app command for this migration, so do not improvise an
unreviewed SQL update. Count, sample, write a reviewed one-off patch, rehearse
it on a restored copy, and verify GL impact before running it live.

## Phase 3: accounting and stock dimensions

In ERPNext Desk, create leaf records under the shared Company:

- Cost Center: `Dambulla`
- Cost Center: `Kurunegala`
- Warehouse: `Dambulla`
- Warehouse: `Kurunegala`

Do not select a group Cost Center or group Warehouse. Existing generic
warehouses such as `Goods In Transit` are not restaurant stock locations.

Open `/admin#branches` as the Owner and set Dambulla's Cost Center and Warehouse.
Set the existing Dambulla POS Profile's Restaurant, Cost Center, and Warehouse
to those same Dambulla records. Close or resolve stale open shifts before moving
a till.

An open shift deliberately outranks an Owner/Admin's selected working branch.
This prevents a Dambulla drawer from changing into a Kurunegala till halfway
through a shift. To switch an Owner/Admin's POS: close the current shift with
its reconciliation, open `/staff`, choose the other Restaurant, open POS, and
start a new shift using that branch's POS Profile. Never bypass this by editing
the opening entry's status or by moving its POS Profile while it is open.

After a shift opens successfully, the POS reloads itself because its menu,
tables and service charge are server-rendered from the newly opened profile's
Restaurant. This is a page-context refresh, not a Redis/Frappe cache problem;
do not add a per-shift `clear-cache` operation. The POS header must visibly name
the active Restaurant so the cashier can confirm the outlet before taking a
sale.

## Phase 4: create Kurunegala

Only after Phase 2 is clean:

1. Open `/admin#branches` and add `Kurunegala`, selecting its leaf Cost Center
   and Warehouse. Company, default customer, service charge, and initial menu
   settings are copied from the existing restaurant by the app.
2. Create a dedicated Kurunegala POS Profile. Set its Company, Restaurant,
   Cost Center, Warehouse, Price List, payment methods, and intended users.
3. Create/confirm a Kurunegala `Restaurant Menu`. Review every category, item,
   price, tax, availability flag, and service charge. Do not assume a copied
   active-menu link is sufficient.
4. Use `/admin#tables` with Kurunegala selected to create its physical tables.
5. Print new QR codes from `/qrcodes`. Test that every QR resolves to the
   Kurunegala restaurant and cannot select a Dambulla table.
6. Use `/admin#staff` to restrict each Kurunegala manager and floor user to the
   correct Restaurant. Decide explicitly how staff working at both outlets
   should be handled; never give a branch manager unrestricted Owner-like reach.

## Phase 5: verification before opening

Run:

```bash
bench --site <live-site> execute luuvcrm.branch.unbranched
bench --site <live-site> execute luuvcrm.branch.check
```

Expected results are `{}` and `[]`/no problems. Then test with separate Owner,
Dambulla-manager, Kurunegala-manager, cashier, kitchen, waiter, and Guest
sessions:

- Owner can switch branches and compare both.
- Each manager sees only their branch's orders, purchases, expenses, reports,
  staff, and tables.
- A Kurunegala sale posts to the Kurunegala Cost Center and Warehouse.
- A Dambulla sale still posts to the Dambulla Cost Center and Warehouse.
- Public menu/order URLs ask for a branch when ambiguous; table QR codes choose
  the physical table's Restaurant.
- Kitchen and waiter screens cannot see the other branch's tickets/tables.
- Stock movement and P&L can be reported separately and together.
- No new row in the seven LUUV branched doctypes has an empty `restaurant`.

Keep `apply_strict_user_permissions` off for the initial observation period.
Re-run both branch checks after real transactions from each role.

## Inter-branch kitchen requests

Kurunegala can request prepared food or components (for example sauces) from
the Dambulla kitchen through `/kitchen` → **Branch Requests**. This feature is
available to Kitchen Staff, Restaurant Manager, Restaurant Owner, and system
administrators; it is intentionally hidden from waiter and cashier accounts.

Workflow:

1. At Kurunegala, confirm the header says `Kurunegala`, open **Branch
   Requests**, choose **New Request**, set the supplying kitchen to Dambulla,
   add item quantities/UOMs, required time, priority, and notes, then send it.
2. At Dambulla, the request appears under **Incoming to this kitchen** within
   the normal eight-second refresh cycle. Move it through **Accepted →
   Preparing → Ready → Dispatched**.
3. At Kurunegala, confirm delivery with **Received**. Both branches can expand
   **Status history** to see who made each change and when.
4. Kurunegala may cancel only while the request is Requested or Accepted.
   Dambulla may reject while it is Requested or Accepted. Invalid status jumps
   and changes by the wrong branch are rejected by the server.

The request is a production and dispatch record only. It does **not** create a
Stock Entry or move quantities between the Dambulla and Kurunegala Warehouses.
Add that as a separately verified inventory workflow if warehouse-level stock
must be transferred automatically.

Deployment adds three standard DocTypes, so code deployment must be followed
by the normal schema and permission steps:

```bash
bench --site <live-site> migrate
bench --site <live-site> execute luuvcrm.setup_doctype.execute
bench --site <live-site> clear-cache
```

Before opening, make one test request with a non-stock-impacting item and walk
the complete status flow from separate Kurunegala and Dambulla kitchen users.
Confirm a user assigned to a third branch cannot list or update it.

## Rollback boundary

If a pre-opening verification fails, stop using the new Kurunegala till and QR
codes. Do not delete submitted accounting documents. Restore configuration from
the recorded preflight values or restore the verified backup if data was bulk
changed. A code rollback does not undo migrated database rows; database rollback
must use the matching backup and should happen only with explicit approval.

## Local rehearsal recorded on 2026-10-20

- Bench site: `test.localhost`
- This is dummy/test data, not a live clone.
- Initial Restaurant: `New Rest`; Company: `sss` (`S`); POS Profile: `Test`.
- Initial unbranched data: 34 `LF Activity Log` rows.
- A full files + database backup was taken before the rehearsal.
- All 34 rows were backfilled before the second Restaurant was created.
- `New Rest` was renamed to `Dambulla`; Frappe updated its linked menu, tables,
  and Restaurant User Permissions.
- Dedicated leaf dimensions were created: `Dambulla - S` and `Kurunegala - S`
  Cost Centers plus same-named Warehouses.
- POS Profiles now map `Test` to Dambulla and `Kurunegala POS` to Kurunegala,
  with the matching Cost Center and Warehouse on each.
- `Kurunegala Menu` was cloned from the small test menu and made active for the
  new branch. Three enabled four-seat tables were created: `Kurunegala-01` to
  `Kurunegala-03`.
- `branch.unbranched()` is clean. `branch.check()` reports no Cost Center,
  Warehouse, till, or branchless-row problem; it only reports pre-existing
  `lf-test-*` Restaurant Manager fixture users that have no Restaurant User
  Permission. Do not copy those fixture users to live.
- The local app order was initially wrong (`luuvcrm` before `erpnext`), making
  both `/order?r=Dambulla` and `/order?r=Kurunegala` return 500 from ERPNext's
  page. Moving `luuvcrm` to the end of the site's installed-app list and
  clearing cache fixed both routes to 200.
- `/menu?r=Dambulla`, `/menu?r=Kurunegala`, `/order?r=Dambulla`, and
  `/order?r=Kurunegala` all return 200 with the correct branch heading.
- `tables.for_picker("Kurunegala")` returns only the three Kurunegala tables;
  `tables.for_picker("Dambulla")` returns only Dambulla's enabled tables.
- Focused verification passed: all 5
  `TestNoPageIsShadowedByAnotherApp` tests and all 13 `test_tables` tests.
- The POS now reloads automatically after opening a shift and visibly names the
  active Restaurant in its header; both focused `TestShiftScreen` tests pass.
- Waiter, Kitchen/KOT, Manager, and Purchases headers also visibly name the
  Restaurant returned by `branch.viewing()`, so staff can confirm the active
  outlet before taking or acting on branch-sensitive work.
- The focused tests wrote two branchless `LF Activity Log` rows for fixture
  orders at `Dambulla-08` and `Dambulla-10`. They were inspected first and then
  explicitly backfilled to Dambulla. This is why the final unbranched check must
  be run after tests and smoke traffic, not only before them.
- These results prove the workflow on dummy data, not the correctness of the
  live database. Repeat every discovery, backup, and verification step live.
