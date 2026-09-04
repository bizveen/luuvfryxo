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

### 2a · Settle the role model

Today four role sets are defined in three files, and roles are created ad-hoc inside `setup_*` endpoints at four call sites.

```
api.py:145        POS_ROLES          = System Manager, POS User, Sales User, Cashier, Administrator
api.py:5993       MENU_MANAGER_ROLES = System Manager, Administrator, Restaurant Manager
activity.py:434   MANAGER_ROLES      = System Manager, Administrator, Restaurant Manager   ← duplicate
www/manager.py:6  MANAGER_ROLES      = System Manager, Administrator, Restaurant Manager   ← duplicate
```

- [ ] Write down the intended roles and what each may do. Proposed, from how the apps actually behave, plus the Owner the multi-branch goal needs:

  | Role | Who | Uses | Must not |
  |---|---|---|---|
  | **Kitchen Staff** | Kitchen | `/kitchen` — advance order status | Everything else |
  | **Waiter** | Floor | `/waiter` — take and amend own orders | Take payment, see the day's totals |
  | **Cashier** | Counter | `/pos2` — sell, take payment, open/close shift | See reports, approve anything |
  | **Restaurant Manager** | Branch manager | `/manager`, `/purchases`, `/activity`, `/errors`, menu, approvals — **for their own branch** | See other branches, change schema |
  | **Restaurant Owner** | Owner | Everything a manager can, **across every branch**, plus consolidated reporting | Change schema |
  | **System Manager** | Developer / installer | Frappe's own admin role: create doctypes, migrate, edit permissions, manage users | — *not a business role* |

  **On System Manager.** It is Frappe's built-in technical administrator role, not something this app invents — every Frappe site has it. It means "can change how the system works": run migrations, add fields, grant permissions, create users. It is for whoever installs and maintains the software. **The restaurant owner should not have it** — they want to see money, not edit doctypes, and a System Manager can bypass every guard in this roadmap. Owner is a separate business role; keep the two apart.

- [ ] Collapse the four sets into one module, `luuvcrm/roles.py`, with one guard per role. Delete the duplicates.
- [ ] Decide whether `Sales User` and `POS User` are still meant to be POS roles or are ERPNext leftovers. `Sales User` appears exactly once in the whole app.
- [ ] Stop creating roles at runtime — they become fixtures in Phase 3.
- [ ] Resolve the `Cashier` gap: `POS Order` grants write to System Manager, POS User, Waiter, Kitchen Staff and Restaurant Manager but **not** Cashier, which is why `update_order` still carries `ignore_permissions`. Fix the DocPerms, then remove the flag.

### 2b · Settle the routes

Every page and its current gate:

| Route | Gate today | Should be |
|---|---|---|
| `/pos`, `/pos2`, `/pos3` | login + POS roles | **one** POS route |
| `/waiter` | login only | + Waiter role |
| `/kitchen` | login only | + Kitchen Staff role |
| `/purchases` | login only | + Manager role |
| `/qrpromo` | login only | + Manager role |
| `/guide` | login only | token auth is correct; drop the session redirect |
| `/manager`, `/activity`, `/errors` | login + Manager roles | ✅ correct |
| `/admin` | login + roles | ✅ correct |
| `/qrcodes` | **public** | + Manager role — it lists every table's kiosk URL |
| `/cus` | guest, token auth | ✅ correct |
| `/customer` | public | **delete** — superseded by `/cus` |
| `/myorders`, `/my_orders` | public | keep **one**, delete the other |
| `/kiosk`, `/order`, `/track`, `/menu`, `/home`, `/claim`, `/pos-login` | public | ✅ correct |
| `/app2` | public | **delete** — see below |

- [ ] Add the four missing role gates (`/waiter`, `/kitchen`, `/purchases`, `/qrpromo`) and fix `/qrcodes`.
- [ ] **Delete `/app2`.** It re-hosted the Frappe desk to work around `luuvcrm/www/app.py` shadowing `/app`. That file no longer exists, and none of the three hooks its own docstring says it needs (`website_route_rules`, `on_session_creation`) are wired in `hooks.py` — so `redirect_desk_login` is dead code and the route is vestigial.
- [ ] Delete `/customer`, and one of `/myorders` / `/my_orders`. Check the live site's traffic first to see which one customers actually reach.
- [ ] **Collapse the three POS pages.** `/pos2` is live, `/pos` is superseded, `/pos3` is a staging copy whose own docstring says it is throwaway — and which is now the largest file in the project. Decide which survives, port anything missing, delete the other two, and drop `pos3` from `KNOWN_APPS` in `activity.py`.
- [ ] Write the surviving route table into `DEVELOPER_GUIDE.md` so the next person does not have to reverse-engineer it.
- [ ] Test: one test asserting every page module's context builder enforces its expected gate. This is the test that stops a new page shipping ungated.

---

## Phase 2c — Branch scoping (the Owner requirement)

Wanted: 2–3 restaurants, an Owner who sees all of them, a manager who sees only their own. **This is mostly a data-model job, not a role job** — a role can only restrict what the data can distinguish, and right now nothing can be distinguished.

Three gaps, all confirmed in the current tree:

1. **No transaction carries a branch.** `POS Order`, `Purchase Bill`, `Expense`, `Wastage` and `POS Approval` have no restaurant field at all. Only `Restaurant Table` links to `Restaurant`, so a dine-in bill is attributable to a branch *indirectly, through its table* — and a takeaway or delivery order has no table, so it is attributable to nothing.
2. **14 call sites take "the first restaurant, whichever it is"** — `frappe.get_value("Restaurant", {}, "name")` in `api.py:13, 115, 3082, 6002` and in nine page context builders. With two restaurants installed, every one of these silently picks one.
3. **Permissions are bypassed on every read.** The app calls `frappe.get_all` 51 times and `frappe.get_list` zero times. `get_all` forces `ignore_permissions=True`, so Frappe's User Permission mechanism — the native way to pin a manager to one branch — would have no effect whatsoever.

Point 3 is the one that matters most: the obvious solution does not work here until the reads change.

- [ ] **Decide what a branch *is*.** Recommended: **one ERPNext Company per restaurant**, with the `Restaurant` record linking to it. ERPNext is natively multi-company, so invoices, expenses and P&L scope themselves — the alternative, scoping by `Restaurant` alone, leaves the accounting side unscoped and has to be hand-built.
- [ ] Add a `restaurant` (Link → Restaurant) field to every transactional doctype, defaulted from the session's POS Profile rather than asked for.
- [ ] Replace the 14 "first restaurant" lookups with the branch resolved from the signed-in user.
- [ ] Add one `_branch_filter()` helper and apply it to every read. Prefer this over flipping all 51 calls to `get_list`: `get_list` would also start enforcing DocPerms everywhere at once, which is a much larger and less predictable change.
- [ ] Give Restaurant Manager a User Permission on their branch; give Owner none, so Owner sees everything.
- [ ] Make the manager reports branch-aware, with an all-branches roll-up for Owner.
- [ ] **Test: a two-restaurant fixture, and an assertion that a manager of branch A sees no order, bill, expense or approval from branch B.** That test is what makes the Owner role real rather than cosmetic.

**Do this before Phase 3, not after.** Adding a branch field to 24 doctypes is cheap while they are being written into the repo, and a migration on live data afterwards.

---

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

- [ ] CI on every push: `run-tests --app luuvcrm`, `ruff`, `eslint`. (`ruff` is not currently installed on this machine.)
- [ ] Unit tests for the money paths, which have none: `_resolve_item_rate`, `_service_charge_on`, split payments, shift open/close, the approval state machine, guide cashback.
- [ ] A fresh-install smoke test in CI (Phase 3's acceptance test).
- [ ] Fix `luuvcrm/test_child_parent.py` — a scratch script pointing at a hard-coded site named `clothing`, sitting where a test should be. Delete or convert.

---

## Phase 5 — Make it fast

- [ ] **P1** Kill the N+1s. Start with `get_manager_dashboard` (`api.py:4423`), which loads every invoice of the day in full just to sum its payments, on a polling screen. Then `get_manager_day_report`, `customer_get_orders`, `get_table_orders`, `get_my_orders`, `get_ongoing_orders`.
- [ ] **P2** Move activity logging off the request path — `frappe.enqueue`, or a direct parameterised INSERT instead of a full ORM document plus its own `db.commit()`.
- [ ] **P3** Add limits to the 50 unbounded `frappe.get_all` calls.
- [ ] **P4** Add indexes on `POS Order.mobile`, `.restaurant_table`, `.pos_invoice`, `Purchase Bill.status`, `LF Activity Log.flag`. Confirm with `EXPLAIN` first.
- [ ] **P5** Extend `www/sw.js` to the pages that are actually live — it caches `/pos`, not `/pos2`. Add an asset build; `pos3.html` is 220 KB unminified.
- [ ] **P6** Pause the 30 polling timers on `document.hidden`.

---

## Phase 6 — Make it a product

- [ ] One brand, read from `LuuvCRM Settings`. Today the app is `luuvcrm`, `hooks.py` says "Zeloura Collective — Network Fashion Platform", and every page says "Luuv Fryxo". A second customer cannot be called Luuv Fryxo.
- [ ] Split `api.py` (7,095 lines) into a package, keeping the public method paths stable via re-exports.
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
