# LuuvCRM — Production Readiness Report

**Reviewed:** 2026-09-04 · commit `f7ff353` · branch `main`
**Reviewer:** engineering audit of the full app (27k lines across `api.py`, `activity.py`, 25 web pages)
**Verdict:** **Not ready to hand to a third party as-is.** The product works — it is genuinely feature-rich and clearly battle-tested at one site — but it cannot currently be *installed* anywhere else, and several endpoints are open in ways that would fail any customer security review.

Two things gate everything else:

1. **The database schema does not exist in the repository.** 24 of the 26 DocTypes the app needs are created at runtime by HTTP endpoints. A fresh `bench install-app luuvcrm` produces a broken site.
2. **Authorisation is inconsistent.** Manager financial reports, and 25 schema-mutating `setup_*` endpoints, are callable by any logged-in user. Three order-entry paths accept the item price from the client.

Everything in this report is reproducible against the current tree; file and line references are exact.

---

## 1. What the product is today

| Surface | Route | Audience | State |
|---|---|---|---|
| POS | `/pos` | Cashier | Production — one page since the three collapsed; `/pos2` and `/pos3` are deleted |
| Waiter PWA | `/waiter` | Floor staff | Production |
| Kitchen display | `/kitchen` | Kitchen | Production |
| Self-order kiosk | `/kiosk` | Customer | Production |
| Manager phone app | `/manager` | Owner/manager | Production |
| Purchases & approvals | `/purchases` | Manager | Production (newest module) |
| Customer PWA | `/cus` | Customer | Production |
| Tour-guide portal | `/guide` | Partner guides | Production |
| QR promo admin | `/qrpromo`, `/claim` | Marketing | Production |
| Activity trail | `/activity` | Manager | Production |
| Front-end error log | `/errors` | Developer | Production |
| Admin launcher | `/admin` | Manager | Production |
| Public site | `/home`, `/menu`, `/order`, `/track` | Public | Production |
| Duplicates | `/myorders` + `/my_orders`, `/customer` + `/cus` | — | **Two live copies of the same screen** |

Backend: 156 whitelisted endpoints (144 in `api.py`), 36 of them guest-accessible.

**Dependency chain, currently undeclared anywhere:** `frappe v15.119` → `erpnext v15.120` → `hospitality` (supplies `Restaurant`, `Restaurant Table`, `Restaurant Menu`) → `luuvcrm`. `pyproject.toml` lists **no dependencies at all**. Installing on a clean bench without ERPNext and Hospitality fails at import.

---

## 2. Blockers — must fix before any third party gets this

### B1 · The schema is not in the repo — *half fixed*

Only `POS Order` and `POS Order Item` ship as DocType JSON. These 24 are built by HTTP calls at runtime:

`Customer Account`, `Customer Token`, `Loyalty Customer`, `Loyalty Settings`, `Cashback Transaction`, `Item Review`, `Kitchen Group`, `POS Cash Movement`, `POS Item Addon`, `POS Approval`, `Purchase Bill`, `Purchase Bill Item`, `Expense`, `Expense Category`, `Wastage`, `QR Promo Campaign`, `QR Promo Code`, `Tour Guide`, `Guide Token`, `Guide Invite Code`, `Guide Settings`, `Guide Cashback Txn`, `LF Activity Log`, plus ~24 `create_custom_field` calls on ERPNext doctypes.

Consequences: a new install is a manual ritual of calling ~27 endpoints in an undocumented order; there is no migration path when a field changes; `bench migrate` does nothing; two sites will silently drift apart; you cannot roll back.

**Done.** All of it is declared in `luuvcrm/setup_doctype.py` — 24 DocTypes, 32 custom fields, 2 property setters, the roles and DocPerms (from `roles.py`, which owns them), the branch column, and the reference rows the screens cannot work without. `after_install` runs it, so `bench install-app luuvcrm` provisions a fresh site on its own, and `report_drift()` reports read-only what a given site is missing. Verified against this site: **zero drift, and re-running changes nothing.**

Three definitions were wrong and are corrected against what a working site actually has:

- `Item.kitchen_group` is a Link to `Kitchen Group`, not `Data` — `get_menu()` resolves it with `frappe.db.get_value("Kitchen Group", …)`.
- `POS Order.order_type` is a Select of `Dine In / Take Away / Delivery`, not `Data` defaulting to `Dine-In`, which `_is_takeaway()` never matched — so take-away orders were charged service charge.
- `Sales Invoice.pos_order` was missing entirely. `_link_invoice_to_order()` writes `pos_order` to whichever invoice type the sale became, and `pos_order.json` declares a Document Link through that fieldname, so leaving it off made Frappe reject the field with `Unknown column 'tabSales Invoice.pos_order'`.

**Still open.** The DocTypes are created `custom: 1`, matching the live site, so they live in the DB and `bench migrate` still ignores them — no migration path when a field changes, and no rollback. Finishing it:

1. Run `report_drift()` against **production** once, to confirm. The definitions were written against the live database — read from the host, not inferred from the `setup_*` code — which is where the three corrections above came from. So this is a check before an irreversible step, not a reconstruction: the drift should be nil or close to it, and anything that does turn up is a field somebody added by hand in the Desk since.
2. Convert `DOCTYPES` to DocType JSON under `luuvcrm/zeloura/doctype/*/` with `custom: 0`, and the custom fields into numbered `patches.txt` entries.
3. Delete the `setup_*` endpoints from `api.py`.

Step 1 must happen before step 2: a DocType JSON that disagrees with the live table is applied by the next `bench migrate`, silently.

### B2 · Duplicate, conflicting `POS Order` definition (critical)

`luuvcrm/doctype/pos_order/pos_order.json` and `luuvcrm/zeloura/doctype/pos_order/pos_order.json` both declare DocType **`POS Order`** in module **`Zeloura`**, and they disagree:

| | `luuvcrm/doctype/` | `luuvcrm/zeloura/doctype/` |
|---|---|---|
| `autoname` | `field:title` | `naming_series:` (`POS-`) |
| invoice link target | `POS Invoice` | `Sales Invoice` |
| field count | 15 | 16 |

`modules.txt` names `Zeloura`, so `luuvcrm/zeloura/` is the intended home and `luuvcrm/doctype/` is stale — but both are on disk and which one wins a migrate is not something to leave to chance. **Delete `luuvcrm/doctype/`** (and its `__pycache__`) after confirming the live schema matches `zeloura/`.

### B3 · No tests, no CI

Zero test files (`test_child_parent.py` is a scratch script that connects to a hard-coded site named `clothing`). No `.github/`. For money-handling code — invoices, shifts, cashback, approvals — this is the risk that will bite hardest after handover, because nobody will know what a change broke.

**Minimum bar before handover:** unit tests for `_resolve_item_rate`, `_service_charge_on`, split payments, shift open/close, and the approval state machine; one CI job running `bench --site test.localhost run-tests --app luuvcrm` plus `ruff` and `eslint`.

### B4 · Undeclared dependencies

Add to `pyproject.toml` / a `.bench-app` manifest: erpnext `~=15.0`, hospitality. Pin the versions you have tested against and say so in the README.

---

## 3. Security findings

Ordered by what a customer's auditor would flag first.

### S1 · `update_order` is open to the internet (critical)

`luuvcrm/api.py:1856` — `@frappe.whitelist(allow_guest=True)`, then `pos_order.flags.ignore_permissions = True; pos_order.save()`.

Any anonymous caller who knows or guesses an order name (`POS-#####` — sequential and guessable) can rewrite that draft order's entire item list, quantities, **prices**, customer name, phone and notes, and the code will then push the same rewrite into the linked POS Invoice. There is no session check, no ownership check, no table check beyond an optional client-supplied `expected_table`.

**Fix:** drop `allow_guest`, require `_require_pos_role()`, and stop passing `ignore_permissions`.

### S2 · Client-supplied prices on all three order paths (critical)

`place_order` (`api.py:~370`), `update_order` (`api.py:1908`), `kiosk_place_order` (`api.py:~2095`) all do:

```python
rate = float(item_data.get("rate", 0))
if not rate:
    rate = _resolve_item_rate(item_code, 0, _get_pos_price_list())
```

The server only looks up the real price when the client sends a falsy one. Post `rate: 1` for a Rs. 1600 pizza and the invoice is issued at Rs. 1. Via `kiosk_place_order` this is reachable **without logging in**.

**Fix:** always resolve the rate server-side from the price list. Accept a client rate only behind an explicit, role-gated, activity-logged "manual price override" path.

### S3 · `customer_get_orders` leaks any customer's history by phone number (critical)

`api.py:2856`. The token branch is fine, but:

```python
if not token:
    mobile = data.get("mobile", "").strip()   # "backward compatibility"
```

…then returns every order for that number — name, items, totals, table, timestamps — with no authentication at all. Sri Lankan mobile numbers are a 10-digit space with a known prefix; this is enumerable. It is a straightforward personal-data breach under any privacy regime the client is likely to face.

**Fix:** delete the fallback. Require the token.

### S4 · Manager financial reports have no role guard (high)

These are `@frappe.whitelist()` with **no** permission check, so any authenticated user — a waiter, a kitchen tablet, any Website User — can call them:

`get_manager_dashboard`, `get_manager_sales_report`, `get_manager_day_report`, `get_manager_monthly_sales`, `get_manager_bill_detail`, `get_manager_purchase_report`, `get_expenses`, `get_due_bills`, `get_purchase_bills`.

Together these expose full revenue, payment mix, cash drawer, per-bill detail, supplier bills and expenses.

The `/manager` **page** is guarded only by "not Guest" (`www/manager.py:12`), and its PIN gate is `context.manager_pin = "4321"` (`www/manager.py:24`) rendered straight into client JavaScript as `const MANAGER_PIN = "4321"` (`www/manager.html:530`) — visible in view-source. The code comments correctly call this a soft lock whose real security is the session; the problem is that the session check is only "logged in".

**Fix:** `_require_manager()` on every `get_manager_*` endpoint and on the page context; move the PIN to a settings DocType and verify it server-side (or drop it and rely on roles).

### S5 · 25 schema-mutating endpoints are unguarded (high)

Of 27 `setup_*` endpoints only `setup_waiter_role` and `setup_kitchen_groups` check for System Manager. The rest — including `setup_pos_shift_perms`, which calls `add_permission` / `update_permission_property` to grant the Cashier role write+submit on `Sales Invoice`, `POS Invoice`, `POS Opening Entry`, `POS Closing Entry`, then `frappe.clear_cache()` — are callable by any logged-in user.

The grants are hard-coded, so this is not arbitrary privilege escalation today, but it is an unauthenticated-by-role path that mutates permissions and flushes cache on a live POS. It should not exist as an HTTP surface at all.

**Fix:** these disappear entirely once B1 is done. Until then, put `frappe.only_for("System Manager")` at the top of each.

### S6 · Customer PIN is plaintext, 4 digits, unlimited attempts (high)

`api.py:2830` — `stored = frappe.db.get_value("Customer Account", ..., "pin")` then `stored != pin`. The comment says "PIN is stored as password field" but the comparison is a plain string compare against the stored value, and there is no attempt counter or lockout. 10,000 tries is minutes of scripting; success returns a 7-day session token.

Note the contrast: the **guide** login does this correctly (`_hash_guide_pw` with a per-record salt and `secrets.compare_digest`, `api.py:6187`). Bring the customer path up to the same standard, and add a lockout to both.

### S7 · `get_order_items` and `get_order_changelog` are guest-readable (medium)

`api.py:1626` and `api.py:1982`. Anyone can read any order's line items — and the changelog, which includes the staff user who made each edit. Order names are guessable.

**Fix:** require a session, or scope by the customer token the way `customer_get_orders` should.

### S8 · Token hygiene (medium)

- Customer tokens are cleaned at 7 days, guide tokens at 90 (`api.py:2751`, `api.py:6459`) — but only lazily, when someone next logs in, and there is no revocation on logout or on PIN change.
- `_get_or_create_customer_token` returns the *existing* token, so a PIN reset does not invalidate old sessions.

### S9 · Things that are already right — keep them

Worth stating plainly, because they are the parts an auditor will check first and they pass:

- **All 32 raw SQL calls are properly parameterised.** No injection found anywhere, including the one f-string query (`api.py:78`), where the interpolated fragment is a fixed literal and every value is a bound parameter.
- **CSRF tokens are used** on all 15 pages that POST.
- **Secrets are read via `get_password()`** (`api.py:3295, 3339, 3534`), never hard-coded — the sole exception is the manager PIN in S4.
- **The activity log sanitises secrets** before writing (`activity.py:_sanitize` drops anything matching `password`/`_pin`/`SECRET_KEYS`).
- **The WhatsApp OTP path has a 30-second cooldown, a 5-minute TTL, and an attempt cap** — the customer PIN path should copy it.
- **`waiter_append_items` deliberately avoids `update_order`'s full rebuild**, with a comment explaining exactly why. This is good defensive design.

---

## 4. Performance findings

### P1 · N+1 queries on every list endpoint (high)

19 sites fetch a list then `frappe.get_doc()` each row inside the loop. The worst:

| Endpoint | Line | Pattern |
|---|---|---|
| `get_manager_dashboard` | `api.py:4423` | `get_doc("POS Invoice")` for **every invoice today** just to sum payments |
| `get_manager_day_report` | `api.py:4645` | same |
| `customer_get_orders` | `api.py:2886` | `get_doc("POS Order")` per order, **unbounded** — a regular's history grows forever |
| `get_table_orders` | `api.py:1652` | per order |
| `get_my_orders` | `api.py:1675` | per order |
| `get_ongoing_orders` | `api.py:1792` | per order, **plus `get_doc("Item")` per line** (`api.py:1799`) — N×M |

At 300 covers a day the manager dashboard is ~300 full document loads with all their child tables, on a screen that polls. This is what will make the phone app feel slow first.

**Fix:** one `frappe.get_all` on the child table filtered by `parent in (...)`, grouped in Python. Typically 2 queries instead of 300.

### P2 · The activity log writes and commits on every mutating request (high)

`hooks.py` wires `after_request → luuvcrm.activity.log_request_activity`. For every POST to a `luuvcrm.*` method that is not a `get_`/`setup_`/`debug_`, it builds a full `frappe.get_doc()` and calls `frappe.db.commit()` of its own (`activity.py:242`). That is a second transaction commit — a second fsync — on the critical path of every sale, plus ORM overhead for what is one INSERT.

The design is deliberate and the retention policy is sensible (90 days plain / 1 year flagged, purged daily). The cost is in the write path.

**Fix:** `frappe.db.bulk_insert` or a direct parameterised INSERT instead of the ORM; better, hand it to `frappe.enqueue` on a short queue so the POS response never waits on it.

### P3 · 50 unbounded `frappe.get_all` calls (medium)

Only 18 of 68 query sites set a limit. `customer_get_orders` and several report endpoints will degrade linearly with data age. The recent commit `bb8f5fc` fixed exactly this class of bug for purchase bills ("the list itself stops at the 60 newest") — apply the same treatment across the board.

### P4 · Likely missing indexes (medium)

Hot filters that are custom fields and therefore unindexed unless explicitly added: `POS Order.mobile` (customer history), `POS Order.restaurant_table` (table view, polled), `POS Order.pos_invoice`, `Purchase Bill.status`, `LF Activity Log.flag`. Verify with `EXPLAIN` on the live DB and add indexes in a patch.

### P5 · No asset build (medium)

Every page is one hand-written HTML file with inline `<style>` and up to 7 inline `<script>` blocks. `pos.html` is 220 KB, `manager.html` 144 KB — uncompressed, unminified, re-downloaded whenever the page is not cached. On a restaurant's phone over 4G that is the difference between a 1-second and a 5-second cold start.

There is a service worker (`www/sw.js`) but it caches only `/pos`, `/waiter`, `/kitchen` — not `/manager` or `/purchases`. (It named the right POS route all along; what changed is that `/pos` is now the live one.)

### P6 · Polling everywhere (low, but adds up)

30 `setInterval` timers across 10 pages, several at 1s and 5s. `pos.html` runs 6. On a shared restaurant WiFi with 8 devices this is constant chatter. Long-poll or Frappe's socket.io realtime would cut it dramatically; at minimum, pause timers on `document.hidden`.

### P7 · Two 70 MB APKs on disk

`luuvcrm/public/luuvcrm-print.apk` and `luuvcrm/www/luuvcrm-print.apk` are byte-identical 70 MB files. Correctly gitignored, so the repo is clean — but 140 MB is copied into every deployment. Serve one, from `public/`, and delete the `www/` copy.

---

## 5. Code health

- ~~**`api.py` is 7,095 lines and 316 KB.**~~ **Resolved.** It is `luuvcrm/api/`, 22 modules, one per domain:

| Module | Lines |
|---|---|
| `orders.py` | 807 |
| `guides.py` | 590 |
| `shifts.py` | 570 |
| `reports.py` | 538 |
| `purchases.py` | 490 |
| `printing.py` | 444 |
| `payments.py` | 416 |
| `customers.py` | 403 |
| `approvals.py` | 361 |
| `promos.py` | 334 |
| `loyalty.py` | 324 |
| `__init__.py` | 320 |
| `whatsapp.py` | 290 |
| `schema.py` | 259 |
| `menu.py` | 246 |
| `kiosk.py` | 222 |
| `expenses.py` | 220 |
| `kitchen.py` | 178 |
| `offline.py` | 171 |
| `wastage.py` | 137 |
| `pricing.py` | 128 |
| `wa.py` | 70 |
| `routing.py` | 41 |

  `__init__.py` re-exports all 261 names, so every `luuvcrm.api.<name>` path a page, hook or WhatsApp link calls resolves unchanged.
- ~~**`/pos3` was declared throwaway and is now the largest file in the project.**~~ **Resolved.** pos3 was promoted: all the work that mattered had landed there and was never ported back, so it is `/pos` now, and `pos2.html`/`pos2.py` plus the original `pos.html`/`pos.py` are deleted. One POS implementation.
- **Duplicate pages:** `myorders.html` (530 lines) and `my_orders.html` (311) are both live; so are `/customer` and `/cus`. Pick one of each.
- **Branding is split three ways:** the app is `luuvcrm`, `hooks.py` says `Zeloura` / "Zeloura Collective — Network Fashion Platform", every page says "Luuv Fryxo", and the module is `Zeloura`. For a product you intend to resell, this needs to become one configurable brand read from a settings DocType — a second customer cannot be called Luuv Fryxo.
- **Dev debris in the app package:** 14 one-off scripts (`check_*.py`, `debug_*.py`, `fix_*.py`, `add_seed_data.py`, `populate*.py`, `assign_groups.py`, `migrate_theme_content.py`, `zeloura_setup.py`) sit next to production code. Gitignored, but present on the server. Move to `scripts/` outside the importable package.
- **Dead module:** the `luuvcrm/zeloura_api.py` / `zeloura/` fashion-platform code (`get_page_data`, `get_themes`, `get_landing`, `Zeloura Theme`, `Zeloura Product`) appears to be from a different product entirely. Confirm and remove.
- **`www/menu.html:225` ships a placeholder WhatsApp number** — `wa.me/9470XXXXXXX`. That link is live on the public menu page right now.
- **`CHANGELOG.md` covers only the print feature.** The last 28 commits are not in it.

---

## 6. Recommended plan

### Phase 1 — Make it safe (1–2 weeks)
1. S1, S2, S3 — close the three critical holes. These are small, surgical edits.
2. S4, S5 — add `_require_manager()` / `frappe.only_for("System Manager")` guards.
3. S6 — hash the customer PIN, add lockout to both login paths.
4. Fix `www/menu.html` WhatsApp number.
5. Write regression tests for exactly these paths, so the fixes cannot silently revert.

### Phase 2 — Make it installable (2–4 weeks)
6. B1 — schema into the repo. **The pivotal task.** `setup_doctype.py` + `after_install` are done; what remains is reconciling against the live site, then DocType JSON and `patches.txt`.
7. B2 — delete the duplicate `POS Order`.
8. B4 — declare erpnext + hospitality.
9. Prove it: `bench new-site`, `bench install-app luuvcrm`, working POS with zero manual steps. That test *is* the definition of done.
10. B3 — CI running tests + ruff + eslint on every push.

### Phase 3 — Make it fast (1–2 weeks)
11. P1 — kill the N+1s, starting with the manager dashboard.
12. P2 — move activity logging off the request path.
13. P3, P4 — limits and indexes.
14. P5 — extend the service worker to `/manager` and `/purchases`; minify.

### Phase 4 — Make it a product (3–6 weeks)
15. One brand, read from settings. No "Luuv Fryxo" in code.
16. Collapse three POS pages into one; collapse the duplicate pages.
17. ~~Split `api.py`.~~ Done.
18. Multi-tenant check: confirm every query is site-scoped and nothing assumes a single `Restaurant` record (`api.py:13` does `frappe.get_value("Restaurant", {}, "name")` — the *first* restaurant, whichever that is).
19. Backups, monitoring, an upgrade runbook.

**Realistic total: 7–14 weeks of one experienced Frappe developer.** Phases 1 and 2 are non-negotiable before anyone else runs this. Phases 3 and 4 can follow the first customer.

---

## 7. Ship / don't-ship

| Scenario | Verdict |
|---|---|
| Keep running at Luuv Fryxo as-is | Acceptable — it works, the risk is largely internal, but do Phase 1 now regardless |
| Hand the code to another developer | Only after Phase 2 — today it is not installable |
| Sell to a second restaurant | Only after Phases 1–3, plus the branding work in Phase 4 |
| Offer as SaaS / multi-tenant | Not close — needs all four phases plus a tenancy review |

The engineering here is better than this report's length suggests. The comments explain *why*, not *what*; the tricky decisions (append-only KOT lines, table-swap race, service charge on dine-in only) are reasoned and documented. What is missing is not craft — it is the packaging that turns one restaurant's working system into software someone else can install, trust and pay for.
