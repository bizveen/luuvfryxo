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

- [ ] Write down the intended roles and what each may do. Proposed, from how the apps actually behave:

  | Role | Uses | Must not |
  |---|---|---|
  | **Cashier** | `/pos2` — sell, take payment, open/close shift | See reports, approve anything |
  | **Waiter** | `/waiter` — take and amend own orders | Take payment, see totals for the day |
  | **Kitchen Staff** | `/kitchen` — advance order status | Everything else |
  | **Restaurant Manager** | `/manager`, `/purchases`, `/activity`, `/errors`, menu edits, approvals | Change schema |
  | **System Manager** | Setup and migration | — |

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
- [ ] Confirm nothing assumes a single restaurant — `api.py:13` takes `frappe.get_value("Restaurant", {}, "name")`, whichever record comes first.
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
