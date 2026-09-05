# Roadmap to production

The working checklist for turning LuuvCRM into software a second restaurant can install. Ordered — later items assume earlier ones are done.

**Branch:** all of this happens on `product`. `main` is the live restaurant. Merge to `main` last, and only when explicitly asked.

**Definition of done for every item below:** the change is made, a test covers it, `bench --site test.localhost run-tests --app luuvcrm` passes, and it is committed. An item without a test is not done.

Status: `[x]` done · `[ ]` not started · `[~]` partly done, note says what is left.

---

## Phase 1 — Make it safe ✅

Closed the holes that were reachable from the internet. Commit `22ce0d1`, 22 tests.

- [x] **S1** `update_order` no longer guest-accessible; requires a POS role
- [x] **S2** All 8 order paths price items with `_server_item_rate` — no client-supplied rates
- [x] **S3** `customer_get_orders` is token-only; the bare-mobile fallback is gone
- [x] **S4** Six manager reports require a manager; eight purchases reads require a POS role; `/manager` gates on roles; the hard-coded PIN is gone
- [x] **S5** All 27 `setup_*` endpoints require System Manager
- [x] **S6** Customer PIN salted + hashed, legacy PINs upgrade on sign-in, 5-try lockout
- [x] **S7** `get_order_changelog` staff-only; `get_order_items` returns receipt fields only to guests
- [x] **S8** Customer token expiry enforced on read
- [x] Public menu no longer ships `wa.me/9470XXXXXXX`
- [ ] **S7b** `/track` still enumerable by order name. Needs a signed link (order name + HMAC) instead of a bare name, and `get_order_items` verifying it. *Deferred from Phase 1 — it changes every WhatsApp link already in customers' hands, so it needs the settings work below.*

---

## Phase 2 — Roles and routes

Do this before the schema work: the schema has to declare DocPerms, and it cannot declare them until the role model is decided.

### 2a · Settle the role model ✅

Done — `luuvcrm/roles.py`, commit below. The four role sets that lived in three files are one module; guards are called as `roles.require_pos()` / `roles.require_manager()` so every call site says where the rule comes from. 23 tests in `tests/test_roles.py` create real users and ask Frappe the same question a request would.

- [x] Write down the intended roles and what each may do:

  | Role | Who | Uses | Must not |
  |---|---|---|---|
  | **Kitchen Staff** | Kitchen | `/kitchen` — advance order status | Everything else |
  | **Waiter** | Floor | `/waiter` — take and amend own orders | Take payment, see the day's totals |
  | **Cashier** | Counter | `/pos` — sell, take payment, open/close shift | See reports, approve anything |
  | **Restaurant Manager** | Branch manager | `/manager`, `/purchases`, `/activity`, `/errors`, menu, approvals — **for their own branch** | See other branches, change schema |
  | **Restaurant Owner** | Owner | Everything a manager can, **across every branch**, plus consolidated reporting | Change schema |
  | **System Manager** | Developer / installer | Frappe's own admin role: create doctypes, migrate, edit permissions, manage users | — *not a business role* |

  **On System Manager.** It is Frappe's built-in technical administrator role, not something this app invents — every Frappe site has it. It means "can change how the system works": run migrations, add fields, grant permissions, create users. It is for whoever installs and maintains the software. **The restaurant owner should not have it** — they want to see money, not edit doctypes, and a System Manager can bypass every guard in this roadmap. Owner is a separate business role; keep the two apart.

- [x] Collapse the four sets into one module, `luuvcrm/roles.py`, with one guard per role.
- [~] `Sales User` and `POS User`: both kept for now and documented in `roles.py` as inherited from ERPNext. Being locked out of the till is a worse failure than an over-broad role — decide once the live site's user list can be checked.
- [x] Stop creating roles at runtime — `roles.ensure_roles()`, shipped by `luuvcrm/patches/v1_0/create_roles.py`.
- [x] Add the **`Restaurant Owner`** role (see Phase 2c — it is a manager for every branch).
- [x] **A Cashier could not take a payment or open a shift on a clean site.** `pos_open_shift` inserts the opening under the caller's own permissions, and ERPNext resolves the debtors account while validating a POS Invoice with a bare `frappe.has_permission("Account", ...)` that a document's `ignore_permissions` flag does not bypass. Both grants existed only inside the `setup_pos_shift_perms` endpoint, which somebody had to remember to call — and the `Account` one was missing from it entirely. They live in `roles.ensure_pos_docperms()` now, applied by `luuvcrm/patches/v1_0/grant_pos_permissions.py`; the endpoint calls straight through so the two cannot drift.
- [x] Resolve the `Cashier` gap: `POS Order` granted write to every role except Cashier. DocPerms rewritten for all seven roles; `update_order`'s order save no longer passes `ignore_permissions`. The linked-invoice save still does — POS Invoice is ERPNext's and its perms come from `setup_pos_shift_perms`, which Phase 3 replaces.
- [~] **Get every DocPerm right, not just Cashier's.** POS Order is done; the other 24 doctypes are written at runtime and get their DocPerms when they move into the repo in Phase 3. Phase 2c switches all 51 reads from `frappe.get_all` to `frappe.get_list`, which starts enforcing DocPerms everywhere at once. Whatever is wrong in the permission table becomes a broken screen on that day. This phase is the groundwork that makes that flip safe.

- [x] **Roles the app requires must be *usable*, not merely present.** `ensure_roles()` re-enables a disabled one and says so. Found the hard way: `Restaurant Manager` existed on the test site but was disabled, and `frappe.get_roles()` omits disabled roles — so every manager silently held nothing and an existence check saw no problem.
- [ ] Review `desk_access` on roles that already exist. `ensure_roles()` sets it only on roles it creates, because changing it on an existing role could take away access somebody is using — but Waiter and Kitchen Staff currently have desk access they do not need.

### 2b · Settle the routes

Every page and its current gate:

| Route | Gate today | Should be |
|---|---|---|
| `/pos` | login + POS roles | ✅ correct — one POS route, `/pos2` and `/pos3` deleted |
| `/waiter` | login only | + Waiter role |
| `/kitchen` | login only | + Kitchen Staff role |
| `/purchases` | login only | + Manager role |
| `/qrpromo` | login only | + Manager role |
| `/guide` | login only | token auth is correct; drop the session redirect |
| `/manager`, `/activity`, `/errors` | login + Manager roles | ✅ correct |
| `/admin` | login + roles | ✅ correct |
| `/qrcodes` | **public** | + Manager role — it lists every table's kiosk URL |
| `/cus` | guest, token auth | ✅ correct |
| `/customer`, `/myorders`, `/my_orders` | public | ✅ correct — 301 aliases to `/cus`, kept for links already sent |
| `/kiosk`, `/order`, `/track`, `/menu`, `/home`, `/claim`, `/pos-login` | public | ✅ correct |
| `/app2` | public | **delete** — see below |

- [ ] Add the four missing role gates (`/waiter`, `/kitchen`, `/purchases`, `/qrpromo`) and fix `/qrcodes`.
- [ ] **Delete `/app2`.** It re-hosted the Frappe desk to work around `luuvcrm/www/app.py` shadowing `/app`. That file no longer exists, and none of the three hooks its own docstring says it needs (`website_route_rules`, `on_session_creation`) are wired in `hooks.py` — so `redirect_desk_login` is dead code and the route is vestigial.
- [x] **`/customer`, `/myorders`, `/my_orders`.** They were never duplicate apps — all three already 301 to `/cus`, and had since the PWA moved there. What was actually there was 841 lines of stale HTML: `myorders.html` (530) and `my_orders.html` (311) were copies of the retired order-history page, sitting behind a `get_context` that raises `frappe.Redirect` before a template is ever reached, so they read as live and rendered never. Both are five-line stubs now, matching `customer.html`, and the three redirects share one `redirect_to_cus()` in `cus.py` instead of three copies of the same query-string handling.
  **They stay as aliases, not deletions** — each is a link already in customers' hands: `/customer?phone=` in WhatsApp threads, `?promo=` on printed QR stickers. The WhatsApp auto-reply, which still pointed at `/myorders`, now links `/cus` directly. 5 tests in `test_pages.py`.
- [x] **Collapse the three POS pages.** pos3 was the complete one — the tablet layer, the iMin printer bridge and the Google-Docs print layouts all landed there and were never ported back — so it is what `/pos` now serves. `pos.html`/`pos.py` (superseded) and `pos2.html`/`pos2.py` (previously live) are deleted, `KNOWN_APPS` carries one `"pos"`, and `/admin`'s POS tile, the `get_print_templates` dotted path and the 401/403 redirect all point at `/pos`. Two things fell out of it: the staging **print-only guard is gone** (a switch that silently stops the till taking money has no place on the till), and with it a redeclared `guestCheckClick` that shadowed the real one — the counter had been getting "Send to kitchen first" where pos2 autosaved the ticket. 11 tests in `test_pages.py`.
  - [ ] **Before this reaches the live site:** every counter tablet has `/pos2` bookmarked and will 404. Either re-point the bookmarks during a close, or add a one-line `/pos2` → `/pos` redirect page for one release and delete it after.
- [ ] Write the surviving route table into `DEVELOPER_GUIDE.md` so the next person does not have to reverse-engineer it.
- [~] Test: one test asserting every page module's context builder enforces its expected gate. `test_pages.py` does it for `/pos` — guest redirected to `/pos-login?redirect=/pos`, a Kitchen Staff user refused, a Cashier served. Extend it to the rest once the table above is settled; a gate test written before the decision just gets rewritten.

---

## Phase 2c — Branch scoping, the Frappe-native way

Wanted: 2–3 restaurants, an Owner who sees all of them, a manager who sees only their own.

**Use Frappe's own machinery, not hand-written filters.** The pieces already exist and are the documented way to do this:

| Piece | What it does |
|---|---|
| **POS Profile per branch** | Carries the branch's Warehouse and Cost Center, so stock and P&L separate themselves. Assign each cashier and manager to their branch's profile. |
| **`restaurant` Link field** | One field on each custom doctype, defaulted from the signed-in user rather than asked for. |
| **User Permission** | `User = Manager A`, `Allow = Restaurant`, `For Value = Branch A`. Frappe then filters every doctype with a link to `Restaurant`, automatically. |
| **`frappe.get_list`** | Applies those permissions. `frappe.get_all` forces `ignore_permissions=True`, which is why none of the above works today. |

The Owner needs no special code at all: give them the role and **no** User Permission, and they see every branch.

### What has to be true first

Flipping 51 reads from `get_all` to `get_list` does not only switch on branch scoping — it switches on **DocPerms**, everywhere, at once. So Phase 2a is not optional groundwork, it is the thing that makes this safe:

- Today `POS Order` grants read/write to System Manager, POS User, Waiter, Kitchen Staff and Restaurant Manager — **not Cashier**. Flip the reads before fixing that and the till stops working.
- Any `get_permission_query_conditions` hook from erpnext or hospitality also starts applying.

Get the roles and DocPerms right, then flip. Not the other way round.

### The work

- [ ] **Decide what a branch is.** Recommended: one **ERPNext Company per restaurant**, with the `Restaurant` record linking to it, plus a POS Profile per branch carrying that company's Warehouse and Cost Center. ERPNext is natively multi-company, so invoices, stock and P&L scope themselves.
- [ ] Add a `restaurant` Link field to `POS Order`, `Purchase Bill`, `Expense`, `Wastage`, `POS Approval`, `POS Cash Movement` and `LF Activity Log`. Today **none of them have one** — only `Restaurant Table` links to `Restaurant`, so a takeaway order belongs to no branch at all.
- [ ] Default it from the user's POS Profile on insert. Never a dropdown the cashier has to remember.
- [ ] Replace the 14 `frappe.get_value("Restaurant", {}, "name")` lookups — *the first restaurant, whichever it is* — with the branch resolved from the session (`api.py:13, 115, 3082, 6002` and nine page context builders).
- [ ] Convert the 51 `frappe.get_all` calls to `frappe.get_list`, after the DocPerms above are right. Verified: `get_list` does **not** truncate — `limit_page_length` is `None` unless passed (`db_query.py:380`), despite what its docstring says.
- [ ] Create the `Restaurant Owner` role. No User Permission for it.
- [ ] Give each Restaurant Manager a User Permission on their `Restaurant`.
- [ ] Make the manager reports branch-aware, with an all-branches roll-up that only Owner sees.
- [ ] **Test: a two-restaurant fixture, asserting a manager of branch A sees no order, bill, expense, approval or activity row from branch B — and that Owner sees both.** That test is what makes the Owner role real rather than cosmetic.

### Migrating the live restaurant safely

The live site has months of rows with no branch. Verified behaviour, from `db_query.py:1408`:

```python
if frappe.get_system_settings("apply_strict_user_permissions"):
    condition = ""
else:
    condition = "ifnull(`tabPOS Order`.`restaurant`, '')='' or "
```

**A row whose link field is empty stays visible to everyone.** So:

- [ ] Adding the field to live data is safe on its own — every existing row is NULL, so nothing disappears from anybody's screen. Do this first and let it settle.
- [ ] **Backfill** every existing row to the live restaurant, in a patch. Until that is done, isolation is incomplete: legacy rows leak across branches precisely because they are empty.
- [ ] Verify zero NULLs in every table before relying on the isolation.
- [ ] Keep **`apply_strict_user_permissions` OFF** until the backfill is verified. Turning it on flips that `ifnull` branch off and every un-backfilled row vanishes for every non-Owner at once. Consider turning it on afterwards — with it off, a row that somehow loses its branch silently becomes visible to all.
- [ ] Order of operations on the live site: add field → backfill → verify → create the second Restaurant → assign POS Profiles → grant User Permissions. Never create the second restaurant before the backfill.

**Do all of this before Phase 3.** Adding a branch field to 24 doctypes is cheap while they are being written into the repo, and a live-data migration afterwards.

## Phase 3 — Make it installable

The pivotal work. Nothing after this is possible without it.

- [ ] **B2 first** — delete the duplicate `luuvcrm/doctype/POS Order`, which conflicts with `luuvcrm/zeloura/doctype/` on `autoname` and on whether the invoice link points at `POS Invoice` or `Sales Invoice`. Verify against the live schema before deleting.
- [ ] Export the 24 runtime DocTypes into `luuvcrm/zeloura/doctype/`, with the DocPerms decided in Phase 2a.
- [ ] Convert the ~24 `create_custom_field` calls into numbered `patches.txt` entries.
- [ ] Write `after_install`, following `docs/SEED_DATA.md` §D: verify erpnext + hospitality, create roles, item groups and UOMs, create `LuuvCRM Settings`, POS Profile and payment modes, seed expense categories, print next steps.
- [ ] Create the **`LuuvCRM Settings`** single (`SEED_DATA.md` §B1). Many later items depend on it: brand, manager PIN, WhatsApp number, feature flags, service charge, price lists.
- [ ] Move the demo menu to `luuvcrm/demo/` behind an explicit `bench execute`.
- [ ] **B4** — declare erpnext and hospitality in `pyproject.toml`.
- [ ] Delete every `setup_*` endpoint once its work lives in a patch or fixture.
- [ ] **The acceptance test:** `bench new-site` → `install-app luuvcrm` → a working sale, with zero manual steps and no HTTP `setup_*` call. Automate it in CI. **This is what "installable" means; nothing else counts.**

---

## Phase 4 — Make it verifiable

**310 tests.** `bench --site test.localhost run-tests --app luuvcrm`.

| File | Covers |
|---|---|
| `tests/fixtures.py` | Builders: items, prices, price lists, menu rows, service charge. Each registers its own cleanup. |
| `test_security.py` (22) | Phase 1: the shape of the API surface — decorators, guards, where a rate comes from |
| `test_roles.py` (23) | Real users with real roles, asked the same question a request asks |
| `test_pricing.py` (27) | The whole rate-resolution chain, price validity, take-away, service charge |
| `test_orders.py` (22) | The order lifecycle driven through the endpoints, as a request |
| `test_payments.py` (32) | Paying in full and in parts, the `paid_qty` accounting, every refusal |
| `test_shifts.py` (20) | Opening and closing a till, as a real cashier rather than as Administrator |
| `test_approvals.py` (21) | The approval state machine, self-approval, the buying price book |
| `test_guides.py` (29) | Cashback earned and spent, and the balance in between |
| `test_activity.py` (20) | The staff trail, and what it must never write down |
| `test_purchases.py` (30) | Entering a bill, approving it, and what the vendor is still owed |
| `test_service.py` (35) | The cash drawer, and an order's life between being taken and paid |
| `test_loyalty.py` (27) | Points, referrals and spending a balance — token-gated now |
| `test_customer_auth.py` (56) | The /cus front door: registering, the PIN, the lockout, the token, the WhatsApp code |

- [x] Unit tests for pricing and service charge — the resolver's priority chain, expired/future/customer/buying prices, the menu-row fallback, take-away exclusion, rounding.
- [x] Order lifecycle: totals, client-rate rejection, the `update_order` rebuild preserving `sent_qty` and waiter notes, the table-swap guard, append-only KOT behaviour, and the Phase 1 guards exercised as real users.
- [x] **Payments.** Invoice totals and line carry-over, the service charge as a tax row, payment modes including the Cash+Card split and its empty leg, tendered and change, paying twice refused, and the whole `paid_qty` ledger: accumulation across splits, the order closing on the last line, and every over-billing refusal.
- [x] **Shifts.** Opening submitted rather than left in draft (the bug fixed once already), per-mode floats, a malformed payload degrading instead of stopping trade, a second shift refused, `_ensure_open_shift` opening one and reusing it, closing reconciliation with a short drawer, and the true shift start surviving the close's in-memory widening. `recover_stuck_shift` still has no test — it needs a queue failure to reproduce.
- [x] **Approvals.** Pending on creation, answered once, only approved/rejected accepted, the manager's comment kept — and nobody answering their own request, whatever kind it is.
- [~] **Purchases.** The decision path is covered: approving writes the buying price, a second bill moves it, rejecting leaves it alone, deciding twice is idempotent, and re-buying at the same price is not counted as a change. Still to do: `create_purchase_bill`, the line-by-line approve screen, and the Due list.
- [x] **Guide cashback.** Redemption refused above the balance or above the bill; the balance moving on both sides; a bill crediting a guide once however often it is retried; no earning on a bill the guide paid from their own balance; the per-bill cap keeping the cashier's order; an item's own commission rate beating the guide's; a discount shrinking the commission proportionally; and a broken payload never taking the sale down with it.
- [x] **Activity log.** Every key in `SECRET_KEYS` dropped, plus anything shaped like a password or ending in `_pin` — while `pinned_note` survives, because a suffix match is deliberate. Clipping, nested values flattened rather than lost, the loggers not logging themselves, and `log_activity` never raising whatever it is handed.
- [x] **Customer auth and the WhatsApp OTP.** `customer_create_or_login`, `customer_set_pin`, `customer_verify_pin`, `customer_get_profile`, `customer_update_profile`, `wa_send_otp`, `wa_verify_otp` — 56 tests in `test_customer_auth.py`. The PIN stored salted and per-account rather than as typed; a wrong PIN and an unknown number answering identically so the endpoint cannot be used as a customer directory; the five-try lockout holding even against the right PIN, counting unknown numbers too, and not spilling from one number to another; legacy plaintext PINs signing in and being upgraded in place; the token as the only credential, expiring on read at seven days; blank profile fields not wiping what is there; and the OTP's cooldown, single use, and five-guess limit.
- [~] **Endpoint coverage: 38 of 157 whitelisted endpoints at the start of this pass.** Measured, not guessed — `test_service.py` was the first batch, `test_loyalty.py` the second, `test_customer_auth.py` the third. Remaining, in rough order of risk: promo campaigns (6), the guide app's own endpoints (13), expenses/wastage/food cost (6), manager report arithmetic (6), print payloads (5), menu editing (3), and one test looping every `setup_*` for idempotency.
- [ ] CI on every push: `run-tests --app luuvcrm`, `ruff`, `eslint`. (`ruff` is not installed on this machine.)
- [ ] A fresh-install smoke test in CI (Phase 3's acceptance test).
- [ ] Fix `luuvcrm/test_child_parent.py` — a scratch script pointing at a hard-coded site named `clothing`, sitting where a test should be. Delete or convert.

---

## Phase 5 — Make it fast

- [ ] **P1** Kill the N+1s. Start with `get_manager_dashboard` (`api.py:4423`), which loads every invoice of the day in full just to sum its payments, on a polling screen. Then `get_manager_day_report`, `customer_get_orders`, `get_table_orders`, `get_my_orders`, `get_ongoing_orders`.
- [ ] **P2** Move activity logging off the request path — `frappe.enqueue`, or a direct parameterised INSERT instead of a full ORM document plus its own `db.commit()`.
- [ ] **P3** Add limits to the 50 unbounded `frappe.get_all` calls.
- [ ] **P4** Add indexes on `POS Order.mobile`, `.restaurant_table`, `.pos_invoice`, `Purchase Bill.status`, `LF Activity Log.flag`. Confirm with `EXPLAIN` first.
- [~] **P5** `www/sw.js` caches `/pos`, which is now the live page — that half fixed itself when the three POS pages collapsed into one. Still to do: an asset build. `pos.html` is 220 KB unminified.
- [ ] **P6** Pause the 30 polling timers on `document.hidden`.

---

## Phase 6 — Make it a product

- [ ] One brand, read from `LuuvCRM Settings`. Today the app is `luuvcrm`, `hooks.py` says "Zeloura Collective — Network Fashion Platform", and every page says "Luuv Fryxo". A second customer cannot be called Luuv Fryxo.
- [x] **Split `api.py` into a package.** 7,338 lines became `luuvcrm/api/`, 22 modules, largest `orders.py` at 807. Every one of the 144 whitelisted paths still resolves — `luuvcrm.api.place_order` and the rest are strings in pages, in `hooks.py`, in WhatsApp links already sent, so `__init__.py` re-exports all 261 names and the test asserts `frappe.get_attr` returns the *same function object*, which is what `is_whitelisted` needs. Three import rings turned out to be misplacements and were fixed rather than worked around: `_block_self_approval` is a permission rule and moved to `roles.py`, `promo_status` was a promo endpoint sitting among the customer ones, `PROMO_WA_NUMBER` is the restaurant's WhatsApp number rather than a promo detail and moved to `api/wa.py`. Sending a WhatsApp message now has one seam (`wa._send_wa_reply`) instead of four bound copies — a test that stubbed the sender used to stub only its own module's. 8 tests in `test_api_package.py`, including a 1,000-line tripwire per module.
- [ ] **`log_client_error`, `get_client_errors`, `clear_client_errors`, `resolve_client_error`, `get_my_network` do not exist.** Found while checking that every `luuvcrm.api.*` string in the pages still resolves after the split — these five never resolved, in the package or in the 7,338-line file before it. `lferr.js` is the first script on every staff PWA and posts each front-end error to `luuvcrm.api.log_client_error`; it is fire-and-forget inside a try/catch, so every report has 404'd in silence. `/errors` (a manager page), the `/admin` error badge and `/network` read the others. Either write them — `activity.py`'s own docstring says "`Client Error Log` (api.py) catches what breaks" — or delete the page and the reporter. Not part of the split; nothing was lost in it.
- [ ] Decide the fate of `zeloura_api.py` and the `Zeloura Theme` / `Zeloura Product` fashion-site code. It appears to belong to a different product.
- [ ] Move the 14 `check_*` / `debug_*` / `fix_*` scratch scripts out of the importable package.
- [ ] Serve one copy of the 70 MB print APK, from `public/`; delete the `www/` duplicate.
- [ ] Backups, monitoring, an upgrade runbook, and the production checklist in `DEVELOPER_GUIDE.md` §4.3.

---

## Rules that apply throughout

1. **Work on `product`.** Merge to `main` last, and only when asked.
2. **Every change gets a test**, new or updated, and the suite is run before committing.
3. **Commit finished work.** Do not leave it in the working tree.
4. **Keep this file current.** Tick items as they land; add what is discovered.
