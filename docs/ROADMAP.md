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
- [x] **Get every DocPerm right, not just Cashier's.** Declared in `roles.APP_DOCPERMS` and applied by `patches/v1_0/grant_app_permissions.py`, because the flip to `get_list` makes the permission table load-bearing everywhere at once. It was wrong in the direction that matters, and would have been found the hard way:
  - **A Restaurant Manager could not read `Purchase Bill`, `Expense`, `Wastage` or `POS Approval`** — their own screens — while a Cashier could. Every purchasing and approval screen would have gone blank for the manager on flip day.
  - **`Restaurant Table` was manager-only**, so the till would have lost its table map.
  - Frappe DocPerms do not inherit: `MANAGER_ROLES` being a superset governs this app's `require_*` guards and nothing Frappe does, so every role is named explicitly.
- [x] **Take away the grants that let the guards be walked around** (`roles.APP_DOCPERM_REVOKE`). Frappe exposes every doctype over its generic REST API, where DocPerms are the only thing in the way — so a rule enforced only in an endpoint is advisory. **Verified against the site, not assumed:** with `Cashier: rwcd` on `POS Approval` a cashier could load their own pending request, set `status` to "approved" and save it — skipping `resolve_approval` and `block_self_approval` entirely, the same hole as "a cashier could approve their own discount" through a different door — and could then delete the record of having done it. Same shape on `Purchase Bill` and `Expense`, and `Cashier: rwcd` on `Tour Guide` / `Guide Cashback Txn` meant the till could edit what the restaurant owes a guide. `walkable_docperms()` is the standing check; `revoke_app_docperms()` is deliberately the *only* place this module takes access away, since doing so is how a screen goes blank mid-service.

- [x] **Roles the app requires must be *usable*, not merely present.** `ensure_roles()` re-enables a disabled one and says so. Found the hard way: `Restaurant Manager` existed on the test site but was disabled, and `frappe.get_roles()` omits disabled roles — so every manager silently held nothing and an existence check saw no problem.
- [ ] Review `desk_access` on roles that already exist. `ensure_roles()` sets it only on roles it creates, because changing it on an existing role could take away access somebody is using — but Waiter and Kitchen Staff currently have desk access they do not need.

### 2b · Settle the routes

Every page and its current gate:

| Route | Gate today | Should be |
|---|---|---|
| `/pos` | login + POS roles | ✅ correct — one POS route, `/pos2` and `/pos3` deleted |
| `/waiter` | login + Waiter roles | ✅ correct |
| `/kitchen` | login + Kitchen roles | ✅ correct — it used to render for a guest |
| `/purchases` | login + Manager roles | ✅ correct |
| `/qrpromo` | login + Manager roles | ✅ correct |
| `/guide` | login only | token auth is correct; drop the session redirect |
| `/manager`, `/activity`, `/errors` | login + Manager roles | ✅ correct |
| `/admin` | login + roles | ✅ correct |
| `/qrcodes` | login + Manager roles | ✅ correct — it was public, and it lists every table's kiosk URL |
| `/cus` | guest, token auth | ✅ correct |
| `/customer`, `/myorders`, `/my_orders` | public | ✅ correct — 301 aliases to `/cus`, kept for links already sent |
| `/kiosk`, `/order`, `/track`, `/menu`, `/home`, `/claim`, `/pos-login` | public | ✅ correct |
| `/app2` | — | ✅ deleted |

- [x] **Added the four missing role gates and fixed `/qrcodes`.** `/waiter` → `require_waiter()`, `/kitchen` → `require_kitchen()`, `/purchases` and `/qrpromo` → `require_manager()`, `/qrcodes` → a login redirect it never had plus `require_manager()`. Two were worse than "missing a role check":
  - **`/kitchen` rendered in full to a logged-out stranger.** It set `frappe.local.flags.redirect_location` for a guest and then fell through without raising, so the flag was set and ignored: 200, 27 KB, every open order table by table. The raise is what makes a redirect a redirect.
  - **`/qrcodes` was public and is the table map** — every table and the kiosk URL that orders against it, so anyone who loaded it could order to any table without being in the building.

  9 tests in `test_pages.py`, and the guest path re-checked through the real renderer: all five now 301 to `/pos-login`, except `/waiter`, which answers with its inline login form and builds no menu.
- [x] **Deleted `/app2`.** Verified before removing, not assumed: `luuvcrm/www/app.py` is gone so nothing shadows `/app` any more; neither `website_route_rules` nor `on_session_creation` is wired in `hooks.py`, so `redirect_desk_login` was never called; and `/app2/pos-order` returned 404 against the real renderer, meaning desk navigation inside `/app2` had never worked. A second copy of Frappe Desk, half-wired, for a collision that no longer exists. `app2` dropped from `KNOWN_APPS`.
- [x] **`/customer`, `/myorders`, `/my_orders`.** They were never duplicate apps — all three already 301 to `/cus`, and had since the PWA moved there. What was actually there was 841 lines of stale HTML: `myorders.html` (530) and `my_orders.html` (311) were copies of the retired order-history page, sitting behind a `get_context` that raises `frappe.Redirect` before a template is ever reached, so they read as live and rendered never. Both are five-line stubs now, matching `customer.html`, and the three redirects share one `redirect_to_cus()` in `cus.py` instead of three copies of the same query-string handling.
  **They stay as aliases, not deletions** — each is a link already in customers' hands: `/customer?phone=` in WhatsApp threads, `?promo=` on printed QR stickers. The WhatsApp auto-reply, which still pointed at `/myorders`, now links `/cus` directly. 5 tests in `test_pages.py`.
- [x] **Collapse the three POS pages.** pos3 was the complete one — the tablet layer, the iMin printer bridge and the Google-Docs print layouts all landed there and were never ported back — so it is what `/pos` now serves. `pos.html`/`pos.py` (superseded) and `pos2.html`/`pos2.py` (previously live) are deleted, `KNOWN_APPS` carries one `"pos"`, and `/admin`'s POS tile, the `get_print_templates` dotted path and the 401/403 redirect all point at `/pos`. Two things fell out of it: the staging **print-only guard is gone** (a switch that silently stops the till taking money has no place on the till), and with it a redeclared `guestCheckClick` that shadowed the real one — the counter had been getting "Send to kitchen first" where pos2 autosaved the ticket. 11 tests in `test_pages.py`.
  - [ ] **Before this reaches the live site:** every counter tablet has `/pos2` bookmarked and will 404. Either re-point the bookmarks during a close, or add a one-line `/pos2` → `/pos` redirect page for one release and delete it after.
- [x] **Route table in `DEVELOPER_GUIDE.md` §1** — every page, who it is for, and what stops everyone else, with a note to add a row when adding a page. Also corrected the guide's claim that `/myorders`/`/my_orders` and `/customer`/`/cus` are live duplicates: they are 301 aliases.
- [~] Test: one test asserting every page module's context builder enforces its expected gate. `test_pages.py` covers `/pos`, `/waiter`, `/kitchen`, `/purchases`, `/qrpromo` and `/qrcodes` — for each, a guest turned away, the role the page is for let in, and a role it is not for refused. Still to add once the rows above are decided: `/guide`, `/manager`, `/activity`, `/errors`, `/admin`, and the public pages.

---

## Phase 2c — Branch scoping, the Frappe-native way

Wanted: 2–3 restaurants, an Owner who sees all of them, a manager who sees only their own.

**Use Frappe's own machinery, not hand-written filters.** The pieces already exist and are the documented way to do this:

| Piece | What it does |
|---|---|
| **POS Profile per branch** | Carries the branch's Warehouse and Cost Center, so stock and P&L separate themselves inside the one company. Assign each cashier and manager to their branch's profile. |
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

**Done so far: the safe half.** The field exists, rows are stamped and backfilled,
and nothing asks for "the first restaurant" any more. None of it changes what
anyone can see — on a one-restaurant site every path returns exactly what it
returned before, and there are tests that say so. What is left is the half that
changes visibility, and it is sequenced deliberately: see *What has to be true
first* above.


- [x] **Decided (2026-09-05): one Company, a Cost Center per location.** One owner, two restaurants, one legal entity — so the books and the Chart of Accounts stay together, and staff, stock, sales and data are separated by **Cost Center, Warehouse and User/POS permissions** instead. Company-per-branch was the earlier recommendation here and is *not* what was chosen; consolidated reporting and a single CoA were worth more than native per-company P&L.

  What that costs in code: `POS Profile.company` stops identifying anything, because every till shares one. So the mapping is explicit —

  | Link | Says |
  |---|---|
  | `Restaurant.cost_center` | this branch's share of the shared Chart of Accounts |
  | `Restaurant.warehouse` | where this branch's stock lives |
  | `POS Profile.restaurant` | which branch this till is at |

  All three are installed by the same patch. `branch.current()` resolves a till through `POS Profile.restaurant`, falling back to `POS Profile.cost_center` → the `Restaurant` claiming it (ERPNext's own field, which a site may have set long before ours existed). **`company` is deliberately not a hop** — with one company it would hand back a branch at random.

  ERPNext already puts `POS Profile.cost_center` on the invoices it raises, so sales separate themselves once each branch's profile carries the right one. That is configuration, not code, which is why `branch.check()` exists: every part of this is something somebody does once in Desk, forgets, and then cannot tell from working. It reports a branch with no cost centre, two branches sharing one (everything works and the P&L is one number), a group cost centre where a leaf is needed, a till with no branch, and any rows still unbranched.
- [x] **`restaurant` Link field on all seven doctypes**, in `luuvcrm/branch.py`, installed by `luuvcrm/patches/v1_0/add_branch_field.py`. Read-only: it is stamped, never typed.
  **Watch this one.** `create_custom_field` adds the column itself for a Custom DocType but *not* for one that lives in the repo, where it stays metadata until the next schema sync — and `POS Order` is a repo doctype. It failed exactly that way first time, in the worst available manner: the field was in the meta, so a save set it and the value went nowhere, and a `has_column`-based "is everything branched?" check read a clean zero. `ensure_branch_field()` now calls `frappe.db.updatedb()` for any doctype whose meta has the field and whose table does not, and there is a test for the column rather than the field.
- [x] **Stamped on insert** by `branch.stamp`, wired in `hooks.py` as `before_insert` for all seven. It never raises: a row that fails to get a branch is a reporting problem, a row that fails to save is a cashier who cannot sell.
- [x] **All 14 replaced with `branch.current()`**, which resolves in this order, each step a stronger statement than the next: a User Permission on `Restaurant` → the open shift's POS Profile, through its company → any POS Profile assigned to the user → the only Restaurant, if there is exactly one. There is deliberately no "first of several" step: with two restaurants and nothing to go on it returns `None`, because a wrong branch is worse than no branch — nothing anywhere would say so. A test greps for the old call so it cannot come back.
- [x] **Flipped the reads that are made on somebody's behalf** — 20 of them, over the seven branched doctypes, in the order, kitchen, report, approval, purchasing, expense, wastage, drawer and POS-page reads. Not a blanket conversion of all 85 `get_all` calls, and that distinction is the point:
  - **`get_list` needs a Frappe user.** `customer_get_orders` is `allow_guest` and token-authenticated, as is the guide app; there is no session user for permissions to apply to, so flipping those returns nothing at all. They stay `get_all` deliberately.
  - Internal work — the branch resolver, the activity writer, the `setup_*` installers, the purges — is not "on behalf of" anyone either.

  Verified: `get_list` does not truncate; `limit_page_length` is `None` unless passed.
- [x] Create the `Restaurant Owner` role. No User Permission for it — done in Phase 2a (`roles.ensure_roles()`, `patches/v1_0/create_roles.py`). `branch.current()` already relies on the *absence* of a User Permission meaning "every branch", so the Owner needs no code of its own.
- [x] **`branch.assign(user, restaurant)` / `unassign` / `assigned`.** A User Permission is the entire mechanism — Frappe applies it to every doctype with a Link to `Restaurant` on every `get_list`, so there is no filtering code in this app and there should not be. The Owner needs nothing: no User Permission means no restriction. `check()` now also reports a Restaurant Manager who has none, because that failure is silent — they quietly have Owner's reach.
- [x] **Branch-aware reports.** `get_list` cannot reach them: they are made of raw `frappe.db.sql`, which no permission layer sees, and of ERPNext's `POS Invoice`, which has no `restaurant` field. It carries `cost_center` — put there by the POS Profile that raised it — which is the whole payoff of a branch being a Cost Center: the money separates itself in ERPNext's own ledger and the report only has to say which one it wants. `branch.sql_cost_center()` scopes the invoice aggregates, `branch.sql_restaurant()` the purchasing ones (Purchase Bill is ours and carries the branch directly). The Owner's roll-up needs no code — unrestricted sessions get an empty condition, which is every branch.
  **All three helpers degrade to nothing** when the session is unrestricted, when there is one branch, or when the branch has no cost centre. That last one matters now: every invoice on the live site has an empty cost centre, so a filter that did not degrade would turn every report to zero the day it shipped. `check()` reports the gap instead.
- [ ] **Decide how a public page picks its branch.** *(Parked at the user's request — last thing in the phase.)* `/menu`, `/home`, `/order`, `/kiosk` and `/cus` have no session user, so `branch.current()` falls through to "the only restaurant" and returns `None` the day there are two — a blank menu rather than the wrong one, which is the right failure but not an answer. They need the branch in the URL (a per-branch link, or `/kiosk?table=` already implies one). Surfaced by the resolver; not a problem that existed while there was one restaurant.
- [x] **The two-restaurant test exists and passes.** A manager of branch A sees no Purchase Bill, Expense, Wastage, POS Approval, POS Cash Movement, LF Activity Log or POS Order from branch B; the other manager sees the mirror image; the Owner sees both. No filtering code anywhere in the app — a User Permission and `get_list` are the whole of it. 46 tests in `test_branch.py`.

### Migrating the live restaurant safely

The live site has months of rows with no branch. Verified behaviour, from `db_query.py:1408`:

```python
if frappe.get_system_settings("apply_strict_user_permissions"):
    condition = ""
else:
    condition = "ifnull(`tabPOS Order`.`restaurant`, '')='' or "
```

**A row whose link field is empty stays visible to everyone.** So:

- [x] Adding the field is done and is safe on its own — every existing row is NULL, so nothing disappears from anybody's screen while `apply_strict_user_permissions` is off.
- [x] **Backfill written** (`branch.backfill()`, run by the same patch). It defaults to the only restaurant and refuses to guess once there are two — at that point somebody has to say which of them the history belongs to. On the test site it filled 152 rows across Purchase Bill, POS Approval and LF Activity Log.
  **A bug worth remembering:** the first version selected the empty rows with `{restaurant: ["in", [None, ""]]}`, which cannot match anything — in SQL `NULL IN (NULL)` is NULL, not true — so it reported filling zero rows and `unbranched()` reported a clean all-good. The filter is `["is", "not set"]`, which builds `ifnull(field, '') = ''`. Same failure shape as the missing column above: the check that says "done" is the one that has to be hardest to fool.
- [x] `branch.unbranched()` is that check, and it counts a doctype whose column is *missing* as every row unbranched rather than skipping it — skipping is how a half-installed field reads as a clean zero. Currently `{}` on the test site.
- [ ] Keep **`apply_strict_user_permissions` OFF** until the backfill is verified. Turning it on flips that `ifnull` branch off and every un-backfilled row vanishes for every non-Owner at once. Consider turning it on afterwards — with it off, a row that somehow loses its branch silently becomes visible to all.
- [ ] Order of operations on the live site: add field → backfill → verify → create the second Restaurant → give each Restaurant its Cost Center and Warehouse → point each POS Profile at its Restaurant → `branch.assign()` each manager. Never create the second restaurant before the backfill. `branch.check()` returns an empty list when all of that is done, and names what is missing until then.
- [ ] **Backfill `cost_center` on the existing invoices too.** Every `POS Invoice` on the live site has an empty one, because the POS Profile never carried a cost centre. Until they are filled, historical revenue belongs to no branch — the report helpers degrade to unscoped rather than to zero, so nothing breaks, but the isolation is incomplete in the same way empty `restaurant` links made it incomplete.

**Do all of this before Phase 3.** Adding a branch field to 24 doctypes is cheap while they are being written into the repo, and a live-data migration afterwards.

## Phase 3 — Make it installable

The pivotal work. Nothing after this is possible without it.

- [x] **B2 — deleted the duplicate `luuvcrm/doctype/`.** Verified against the site rather than assumed: the live `POS Order` matches `luuvcrm/zeloura/` exactly — `naming_series:` (names read `POS-00104`), 16 fields, `pos_invoice → Sales Invoice`, the Document Link to Sales Invoice. `luuvcrm/doctype/` was the stale one, and `get_controller` confirmed Frappe never loaded it.
  **Its controller was not identical, and the difference was money.** The orphan excluded take-away lines from the service charge; the one Frappe loads charged the full subtotal. Live totals are right because `api._apply_takeaway_service_charge()` overrides after every save — its docstring says so — but that made the deletion a chance to lose the only correct copy. The exclusion is in the loaded controller now, so anything that saves a POS Order outside that endpoint is no longer wrong in the restaurant's favour.
- [x] **Exported all 24 into `luuvcrm/zeloura/doctype/`, `custom: 0`.** `bench migrate` now syncs them, which is what gives a field change a migration path. `setup_doctype` no longer declares their shape — it carries a roll-call so `report_drift()` can say when one failed to arrive, and `ensure_doctypes()` checks rather than creates, because `install_app` syncs the JSON *before* `after_install` runs.
  **The trap, found by rehearsing the migrate rather than reasoning about it.** `bench migrate` calls `remove_orphan_doctypes()`, which treats a DocType whose controller raises `ImportError` as orphaned **and deletes it**. `import_controller` looks for the class named `doctype.replace(" ", "").replace("-", "")` — so `POS Approval` needs `POSApproval`. Generating controllers with a plain title-case rule gave `PosApproval`, `QrPromoCode`, `LfActivityLog` and four more; the next migrate found seven orphans and deleted them. Tables and rows survived (it is documented as non-destructive) but every field definition, permission and naming rule went. On the live site that is an app that no longer starts. Two tests guard it now, one matching the source and one calling `get_controller` the way migrate does.
- [ ] **Before this reaches the live site:** run `report_drift()` against production. The definitions were written against the live database from the host, so this confirms rather than reconstructs — but a DocType JSON that disagrees with the live table is applied by the next `bench migrate` silently, and this conversion is the point of no return.
- [ ] Convert the ~24 `create_custom_field` calls into numbered `patches.txt` entries.
- [x] **`after_install` written**, running `luuvcrm/setup_doctype.py`: 24 DocTypes, 32 custom fields, 2 property setters, roles + DocPerms (delegated to `roles.py`), the branch column, `LF Client Error`, and the reference rows — kitchen groups, expense categories, the `Walk In` customer, the settings singles. `report_drift()` says read-only what a site is missing. Verified on this site: zero drift, and re-running changes nothing. 11 tests in `test_setup.py`.
  **The thing to remember:** `frappe.installer.set_all_patches_as_completed()` marks every patch as done on a fresh install *without running it*, so none of the four patches in `patches.txt` fires on a new site — `after_install` is all it gets, and it has to do everything they do or a fresh site and a migrated site are different sites.
  Still missing from it, per `SEED_DATA.md` §D: `LuuvCRM Settings`, the POS Profile, payment modes, item groups and UOMs.
- [ ] Create the **`LuuvCRM Settings`** single (`SEED_DATA.md` §B1). Many later items depend on it: brand, manager PIN, WhatsApp number, feature flags, service charge, price lists.
- [ ] Move the demo menu to `luuvcrm/demo/` behind an explicit `bench execute`.
- [x] **B4 — declared, but in `hooks.py`, not `pyproject.toml`.** `required_apps = ["erpnext", "hospitality"]`: pyproject dependencies are pip packages, and these are Frappe apps that bench resolves. `frappe.installer.install_app()` reads this hook and installs each first, so `bench install-app luuvcrm` on a clean bench brings them with it instead of failing at the first import.
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

- [~] **One brand, read from settings.** `luuvcrm/settings.py` is the whole client-specific surface, arranged the way Laravel does it: `site_config.json` is the `.env` (already outside the repo, one file per site, where Frappe keeps the database password), `LuuvCRM Settings` is the row the owner edits in the Desk, and the defaults in `settings.KEYS` are the config file. **Each key has exactly one home**, so changing it in the wrong place cannot silently do nothing. `bench execute luuvcrm.settings.describe` prints every key, where it lives and whether it is still on its default; `site_config.example.json` documents the operator half.
  Done: `hooks.py` no longer announces a different product, the website context serves `brand_name` from settings, and the WhatsApp messages, the business numbers, the pickup outlet and the guide app link all resolve through it — no client domain is left built into a message, and a test greps for it.
  **Still to do: the 21 HTML pages.** They say "Luuv Fryxo" in markup; the context variable is there for them (`brand_name`, `brand_short`), so it is a sweep, not a design question.
- [x] **Split `api.py` into a package.** 7,338 lines became `luuvcrm/api/`, 22 modules, largest `orders.py` at 807. Every one of the 144 whitelisted paths still resolves — `luuvcrm.api.place_order` and the rest are strings in pages, in `hooks.py`, in WhatsApp links already sent, so `__init__.py` re-exports all 261 names and the test asserts `frappe.get_attr` returns the *same function object*, which is what `is_whitelisted` needs. Three import rings turned out to be misplacements and were fixed rather than worked around: `_block_self_approval` is a permission rule and moved to `roles.py`, `promo_status` was a promo endpoint sitting among the customer ones, `PROMO_WA_NUMBER` is the restaurant's WhatsApp number rather than a promo detail and moved to `api/wa.py`. Sending a WhatsApp message now has one seam (`wa._send_wa_reply`) instead of four bound copies — a test that stubbed the sender used to stub only its own module's. 8 tests in `test_api_package.py`, including a 1,000-line tripwire per module.
- [x] **The front-end error log was never wired up.** `log_client_error`, `get_client_errors`, `resolve_client_error` and `clear_client_errors` did not exist — not in the package, not in the 7,338-line file before it. `lferr.js` is the first script on every staff PWA and posts each front-end error to `luuvcrm.api.log_client_error` inside a fire-and-forget try/catch, so every report had 404'd in silence for the reporter's whole life; `/errors` and the `/admin` badge answered nothing. The alarm was off, and an app with the alarm off looks exactly like an app with no errors. Written in `luuvcrm/api/errors.py` against the contract the pages already speak, with the `LF Client Error` DocType installed the same runtime way as `LF Activity Log`. Rows are keyed by a fingerprint of (app, kind, message, source, line) so a render loop on eight tills is one row with a count, not thousands; reads are manager-gated because a row carries a stack trace, a page URL, a user id and a device string; and a daily purge drops fixed rows after 30 days and open ones after 180. 35 tests in `test_client_errors.py`.
- [ ] **`get_my_network` and `/network` belong to the other product.** `network.html` is titled "My Network — Zeloura Collective" and calls `luuvcrm.api.get_my_network`, which does not exist. Left alone deliberately: it is part of the Zeloura fashion-site code whose fate is a Phase 6 decision, and writing the endpoint would be building on something that may be deleted.
- [x] **`lferr.js` on `/waiter` and `/kitchen`.** It was on six pages and missing from the two the floor and the kitchen actually hold. Added with `lfact.js` alongside it, in the same order `pos.html` uses — the CSRF meta first, then the reporters, before any of the page's own script can throw. A test now asserts every staff page loads it and tags rows with an app `KNOWN_APPS` recognises.
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
