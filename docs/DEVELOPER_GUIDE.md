# LuuvCRM — Developer Guide

How to set up, develop, and deploy this app. Written for a developer who has never seen it.

> **Read this first:** the app does not currently install cleanly on a fresh site — most of the schema is created at runtime by HTTP endpoints rather than shipped in the repo. §2 tells you how to work around that today; `PRODUCTION_READINESS.md` §B1 tells you how to fix it properly.

---

## 1. What you are working with

A Frappe v15 app that turns ERPNext + Hospitality into a restaurant operating system: point of sale, waiter and kitchen apps, a self-order kiosk, a customer PWA, purchasing with approvals, a manager phone dashboard, loyalty, WhatsApp ordering and a tour-guide commission scheme.

**Architecture in one paragraph.** There is no front-end framework and no build step. Every screen is a single hand-written HTML file under `luuvcrm/www/` with inline CSS and JavaScript, paired with a `.py` context builder that Frappe's website router calls. Those pages talk to the backend over `fetch` to whitelisted Python methods, almost all of which live in `luuvcrm/api/` — a package of 22 modules, one per domain, whose `__init__.py` re-exports every name so the method paths the pages call are the flat `luuvcrm.api.<name>` they always were. Orders are held as a custom `POS Order` doctype while in progress, then materialised into an ERPNext `POS Invoice` on payment. Vanilla, boring, and easy to debug — with the trade-off that the pages are unminified.

### Layout

```
luuvcrm/
├── hooks.py                  App wiring: request hooks, scheduler, doc events
├── api.py                    ★ 144 whitelisted endpoints — the whole backend
├── activity.py               Staff activity trail (7 endpoints + the after_request hook)
├── place_order_api.py        Guest order entry
├── zeloura_api.py            Fashion-site code — likely dead, see below
├── zeloura/doctype/          POS Order + POS Order Item  ← the live definitions
├── doctype/                  ⚠ stale duplicate of the above — delete after verifying
├── www/                      25 pages, each <name>.html + <name>.py
├── public/                   CSS, PWA icons, the print APK
└── patches.txt               empty
docs/                         this guide and its siblings
```

### Dependency chain

```
frappe v15.119  →  erpnext v15.120  →  hospitality  →  luuvcrm
```

`hospitality` supplies `Restaurant`, `Restaurant Table`, `Restaurant Menu`. **None of these are declared in `pyproject.toml`** — you must install them yourself, in order.

### Things to know before you touch anything

- **The schema ships in the repo.** `luuvcrm/setup_doctype.py` declares every DocType, custom field, property setter, role, DocPerm and the branch column, and runs from `after_install` — a fresh site comes up from `bench install-app luuvcrm` alone, and you no longer call the `setup_*` endpoints by hand. It is create-only and idempotent; `report_drift()` says read-only what a site is missing.
  It has to carry the patches' work too: `frappe.installer.set_all_patches_as_completed()` marks every patch as done on a fresh install *without running it*, so `after_install` is all a new site gets. That is why `execute()` calls into `roles` and `branch` rather than declaring permissions of its own.
  Still open (`PRODUCTION_READINESS.md` §B1): the DocTypes are created `custom: 1`, matching live, so they live in the DB and `bench migrate` ignores them.
- **`/pos` is the POS.** There is one. There used to be three (`/pos`, `/pos2` and the `/pos3` staging copy); pos3 had all the work, so it became `/pos`. **The other two are redirects, not deletions** — a counter tablet is bookmarked to an old address and a 404 mid-service costs a queue. They carry the query string, so `/pos2?table=T4` still opens the till on that table. Read the header comment at the top of `www/pos.html` before editing it — the tablet layer, the printer bridge and the Google-Docs print layouts are all documented there.
- **`/customer`, `/myorders` and `/my_orders` are 301s to `/cus`,** not copies of it. They are kept because each is a link already in a customer's hands. The customer app is `/cus` and nowhere else.
- **The brand is hard-coded.** "Luuv Fryxo" appears in every page; `hooks.py` says "Zeloura"; the app is `luuvcrm`. Same product, three names.
- **`zeloura_api.py` and `zeloura/` clothing code** (`Zeloura Theme`, `Zeloura Product`, `get_landing`) are from a different product. Confirm with the owner before removing.
- **`check_*.py`, `debug_*.py`, `fix_*.py`** are gitignored one-off scripts run against the live DB. Not part of the app.

### The routes

Every page, who it is for, and what stops everyone else. Add a row when you add
a page — an ungated page does not fail, it just works for whoever asks, which is
how `/kitchen` came to render for logged-out strangers and `/qrcodes` to publish
the table map. `test_pages.py` asserts the staff gates.

| Route | Who | Gate |
|---|---|---|
| `/pos` | Cashier at the counter | login + `roles.require_pos()` |
| `/waiter` | Floor staff | login + `roles.require_waiter()` |
| `/kitchen` | Kitchen | login + `roles.require_kitchen()` |
| `/purchases` | Manager — vendor bills, approvals | login + `roles.require_manager()` |
| `/qrpromo` | Manager — sticker batches, the free item | login + `roles.require_manager()` |
| `/qrcodes` | Manager — printable kiosk QR labels | login + `roles.require_manager()` |
| `/manager`, `/activity` | Manager — takings, the staff trail | login + manager roles |
| `/errors` | Front-end error log | login + System Manager |
| `/admin` | **The back-office portal** — overview, reports, restaurants, staff, menu, items, and the index of every sub-app. Sections are hash-routed (`/admin#staff`), so one is linkable and survives a reload | login + Manager roles. Restaurants and Staff are owner-only: the page hides them and every endpoint behind them calls `require_owner()` |
| `/owner` | 301 to `/admin#branches` — the screen is a section of the portal now. Kept because it is bookmarked | — |
| `/guide` | Tour guides | guest page, token auth (`guide_login`) |
| `/cus` | Customers | guest page, phone + PIN token |
| `/customer`, `/myorders`, `/my_orders` | — | 301 → `/cus`, kept for links already sent |
| `/kiosk`, `/menu`, `/track`, `/claim` | Customers, walk-up | public by design |
| `/order`, `/home`, `/` | Customers, walk-up | public by design. **The templates are `order-online.html` and `restaurant-home.html`**: erpnext ships pages called `order` and `home`, and Frappe resolves by `reversed(get_installed_apps())`, so the later app wins the name — `/order` was a 500 and `/home` served erpnext's page. `website_route_rules` in `hooks.py` keeps the public URLs. `/` comes from the `home_page` hook, which beats Website Settings. **Adding a page: check the name is not one erpnext, hospitality or hrms already uses** — `test_pages.TestNoPageIsShadowedByAnotherApp` is the standing check. |
| `/pos-login` | Staff sign-in | public by design |
| `/app` | Frappe Desk | Frappe's own. `before_request` sends only `?promo=` / `?ref=` / `?phone=` deep links to `/cus` |

Two things that are *not* routes of ours and are easy to mistake for one:
`/network` and the rest of the Zeloura fashion pages belong to another product,
and `/app2` — a second copy of Frappe Desk that existed because `www/app.py`
once shadowed `/app` — was deleted once that file was gone.

---

## 2. Local setup

### Prerequisites
Python 3.10+, Node 18+, MariaDB 10.6+, Redis, wkhtmltopdf (patched-qt build, for printing).

### Bench and site

```bash
pip install frappe-bench
bench init frappe-bench --frappe-branch version-15
cd frappe-bench

bench get-app erpnext --branch version-15
bench get-app hospitality
bench get-app https://github.com/bizveen/luuvfryxo.git luuvcrm

bench new-site dev.localhost
bench --site dev.localhost install-app erpnext
bench --site dev.localhost install-app hospitality
bench --site dev.localhost install-app luuvcrm

bench --site dev.localhost set-config developer_mode 1
bench --site dev.localhost clear-cache
bench start
```

### Bootstrapping the schema (the manual part)

Because the DocTypes are created at runtime, a fresh site needs the `setup_*` endpoints run once. Do it from the console, not over HTTP:

```bash
bench --site dev.localhost console
```

```python
import luuvcrm.api as a, luuvcrm.activity as act

for fn in [
    "setup_waiter_role", "setup_pos_shift_perms", "setup_pos_order_invoice_link",
    "setup_takeaway_field", "setup_invoice_takeaway_field", "setup_split_fields",
    "setup_sent_qty_field", "setup_item_notes_field", "setup_kitchen_status_options",
    "setup_scheduled_time_field", "setup_check_printed", "setup_item_addons",
    "setup_kitchen_groups", "setup_kiosk_tools", "setup_customer_accounts",
    "setup_customer_profile_fields", "setup_loyalty", "setup_cash_movement",
    "setup_raw_materials", "setup_purchase_bill", "setup_expenses",
    "setup_pos_approval", "setup_wastage", "setup_qr_promo",
    "setup_tour_guides", "setup_print_sections",
]:
    print(fn, getattr(a, fn)())

act.setup_activity_log()
frappe.db.commit()
```

Order matters in places — field setups before the endpoints that read those fields. If one throws, fix it and re-run: every `setup_*` is idempotent by design.

Then, in the Desk:
1. **Company** — with the right currency (LKR) and abbreviation.
2. **POS Profile** — company, warehouse, currency; attach `Cash` and `Card` as POS Payment Methods.
3. **Restaurant** (Hospitality) — set `service_charge`, create a few `Restaurant Table` records.
4. **Restaurant Menu** — mark it enabled, add items, set it as the restaurant's `active_menu`.
5. **Items** — with valid `Item Price` rows in `Standard Selling`, or the POS shows 0.

Demo data, if you want it:

```bash
bench --site dev.localhost execute luuvcrm.add_seed_data.run
```

### Verify

Log in, then open `/admin` — it links every sub-app. `/pos` should show your menu and tables. If prices are 0, your `Item Price` rows are missing or expired (`_resolve_item_rate`, `api.py:70`, requires `selling=1` and a valid date range).

---

## 3. Developing

### The loop

Python changes need `bench restart` (or run `bench start` in the foreground and let the watcher reload). HTML and JS under `www/` are picked up on refresh — but `no_cache = 1` is set in each context builder, so a hard refresh is usually enough. DocType JSON changes need `bench --site dev.localhost migrate`.

### Adding an endpoint

```python
@frappe.whitelist(methods=["POST"])
def do_the_thing():
    _require_pos_role()                      # ← never skip this
    data = frappe.local.form_dict
    name = (data.get("order_name") or "").strip()
    if not name:
        frappe.throw(_("Order name is required"))
    ...
    return {"ok": True}
```

Then from a page:

```javascript
const r = await fetch('/api/method/luuvcrm.api.do_the_thing', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/x-www-form-urlencoded',
    'X-Frappe-CSRF-Token': CSRF          // rendered by the context builder
  },
  body: new URLSearchParams({ order_name: name })
});
const { message } = await r.json();
```

### House rules the existing code follows

1. **Guard first.** `_require_pos_role()` (`api.py:105`), `_require_manager()` (`api.py:6537`), or `_require_menu_manager()` (`api.py:5819`). If none fits, write one. A bare `@frappe.whitelist()` means *every logged-in user*, including waiters and website customers.
2. **`allow_guest=True` is a decision, not a default.** 36 endpoints have it and at least four should not.
3. **Never trust a client-supplied price.** Resolve it with `_resolve_item_rate`. The three existing order paths get this wrong — do not copy them.
4. **Parameterise every query.** All 32 raw SQL sites do this correctly; keep the record clean.
5. **`ignore_permissions=True` needs a comment explaining why.**
6. **Comment the *why*.** The best code here explains reasoning — see `waiter_append_items` (`api.py:1685`) on why it avoids `update_order`'s rebuild, or the table-swap race guard at `api.py:1877`. Match that standard.
7. **Log money-moving actions** through `luuvcrm.activity.log_activity` so the manager's trail stays complete.

### Debugging

```bash
bench --site dev.localhost console          # interactive
tail -f logs/web.error.log                  # server errors
bench --site dev.localhost mariadb          # SQL
```

- `/errors` is the front-end error log — `www/lferr.js` posts uncaught JS errors there. Manager-only.
- `/activity` is the staff action trail, with per-bill drill-down.
- **Error Log** in the Desk holds Python tracebacks.

### Code style

`pre-commit` runs ruff, eslint, prettier and pyupgrade:

```bash
cd apps/luuvcrm && pre-commit install
```

Python is tabs, double quotes, 110 columns (`pyproject.toml`).

### Tests

622 of them, in `luuvcrm/tests/`. If you add anything to the money path, add the test with it:

```bash
bench --site test.localhost run-tests --app luuvcrm
bench --site test.localhost run-tests --module luuvcrm.tests.test_payments   # one file
```

**Read the summary, not the tail.** `report_drift()` prints after the result, so
`run-tests ... | tail -20` shows you `missing DocTypes: 0` and hides
`FAILED (failures=6)` — and the pipe makes the exit code the tail's. Redirect and
grep instead:

```bash
bench --site test.localhost run-tests --app luuvcrm > /tmp/suite.txt 2>&1
grep -E "^Ran |^OK$|^FAILED|^FAIL:" /tmp/suite.txt
```

**Do not run `main`'s tests against a site `product` is tested on.** `main` still
builds its schema at runtime, so its fixtures recreate DocTypes as `custom: 1`
and take them back out of `bench migrate`'s path.

### The acceptance test

The suite runs against a site that already exists. The question that actually
decides whether this is a product — *can somebody else install it?* — needs a
site that does not:

```bash
MARIADB_ROOT_PASSWORD=... ./scripts/acceptance.sh
```

It creates a throwaway site, installs the app, runs ERPNext's own setup wizard
(the installer's step, not ours), then takes a real sale through the real
endpoints: schema present and not `custom: 1`, roles usable, a menu item added
through the app, a shift opened, two burgers sold and billed, the manager's
dashboard showing the money, and the reference rows the screens need. Then it
drops the site. `--keep-site` leaves it up to look around.

It **reports rather than stops**: every step is caught, so one failure does not
hide the four behind it. What comes back is the list of things a fresh site
still cannot do, which is the remaining Phase 3 work written by the software
instead of guessed at.

---

## 4. Deploying

### 4.1 Current setup (git over an SMB share)

The production host is reached through a mounted Windows share:

```bash
gio mount -a "smb://192.168.1.8/users"       # often not mounted; do this first
git push host main
```

The `host` remote is a **non-bare checkout** with `receive.denyCurrentBranch=updateInstead`, so the push updates the working tree directly. Two things bite regularly:

- If the share is not mounted, git reports "does not appear to be a git repository" — it is a mount problem, not an auth problem.
- If the host's working tree is dirty the push is rejected. It routinely carries whitespace-only diffs (last-line CRLF/LF) in `www/{kiosk,my_orders,myorders,qrcodes,track}.html` and `www/qrcodes.py`. Stash them on the host, then push.

Then, on the host:

```bash
bench --site <site> migrate
bench --site <site> clear-cache
bench restart
```

`origin` (GitHub) is deliberately not used for deploys.

**This is not a production deployment method.** There is no build, no test gate, no atomic switch and no rollback beyond `git reset`. It is fine for one restaurant with the developer nearby; it is not something to hand to a third party.

### 4.2 What deployment should look like

```
push → CI (ruff, eslint, tests) → tagged release → deploy script on the host
```

The deploy script:

```bash
set -euo pipefail
SITE=prod.localhost

bench --site $SITE backup --with-files            # 1. backup first, always
bench --site $SITE set-maintenance-mode on        # 2. drain
cd apps/luuvcrm && git fetch && git checkout "$1" # 3. a tag, not a branch
cd -
bench --site $SITE migrate                        # 4. schema
bench build --app luuvcrm                         # 5. assets
bench --site $SITE clear-cache
bench --site $SITE set-maintenance-mode off
bench restart
bench --site $SITE execute luuvcrm.api.get_menu_data   # 6. smoke test
```

Rollback is the same script with the previous tag, plus `bench --site $SITE restore <backup>` if a migration touched the schema.

### 4.3 Production checklist

Before any site serves real money:

- [ ] `developer_mode = 0`
- [ ] HTTPS with a real certificate — `bench setup lets-encrypt <site>`
- [ ] `encryption_key` set and backed up **separately from the database** (WhatsApp tokens are encrypted with it, and so is the HMAC on every `/track` link; lose it and both are gone)
- [ ] Automated daily backups, restored to a scratch site at least once to prove they work — see 4.4
- [ ] Off-site backup copies
- [ ] Supervisor + nginx via `bench setup production <user>`
- [ ] **gzip on in nginx.** Measured on this app: `pos.html` is 206 KB and 57 KB compressed, all 24 pages 766 KB → 184 KB. It is one config line and it is worth more than any minifier — check it with `curl -sI -H "Accept-Encoding: gzip" https://<site>/pos | grep -i content-encoding` and treat a missing header as a fault.
- [ ] Log rotation
- [ ] A scheduler that actually runs — `bench doctor`. The activity purge, the client-error purge and every reminder depend on it, and a dead scheduler is silent.
- [ ] Firewall: 80/443 only; MariaDB and Redis not exposed
- [ ] Uptime and disk monitoring — see 4.5
- [ ] `apply_strict_user_permissions` **off** until `cost_center` is backfilled on the existing `POS Invoice` rows (ROADMAP Phase 2c). Turning it on before that hides every un-backfilled row from every non-Owner at once.

Phase 1 of the roadmap closed the internet-reachable security holes that used to be listed here; `PRODUCTION_READINESS.md` §3 is the record of what they were.

### 4.4 Backups

`bench --site <site> backup --with-files` writes to `sites/<site>/private/backups/`. Three things make the difference between having backups and having files:

1. **Schedule it.** A cron entry per site, daily, plus one immediately before every upgrade (the runbook in 4.5 starts with one).
2. **Copy it off the box.** A backup on the same disk as the database survives exactly the failures that do not matter.
3. **Restore it somewhere, once.** `bench --site scratch.localhost restore <path>` on a throwaway site is the only thing that turns a backup into a recovery. Do it before the first real service, not after the first real loss.

Back up `encryption_key` from `site_config.json` **separately**. It is not in the database dump, and without it the restored site cannot read a single WhatsApp token or verify a `/track` link.

The database is the money. `--with-files` also carries item images and the print companion APK, which are replaceable; do not let their size become a reason to skip a run.

### 4.5 The upgrade runbook

The order matters, and three of these steps exist because of something this app has already done wrong once.

```bash
SITE=prod.localhost

# 1. Back up first, and prove the file exists before touching anything.
bench --site $SITE backup --with-files

# 2. Rehearse the migrate somewhere else. bench migrate DELETES a DocType whose
#    controller raises ImportError (remove_orphan_doctypes) — a bad class name
#    once took out seven of them. Restore the backup to a scratch site, migrate
#    there, and only then do it on the live one.
bench --site scratch.localhost restore <the backup you just made>
bench --site scratch.localhost migrate

# 3. Drain, then take the code.
bench --site $SITE set-maintenance-mode on
cd apps/luuvcrm && git fetch && git checkout <tag> && cd -

# 4. Schema, assets, cache.
bench --site $SITE migrate
bench build --app luuvcrm
bench --site $SITE clear-cache

# 5. Check the schema agrees with the repo before letting anyone in.
bench --site $SITE execute luuvcrm.setup_doctype.report_drift

# 6. Back into service.
bench --site $SITE set-maintenance-mode off
bench restart

# 7. Smoke test, in this order: a read, a page, a till.
bench --site $SITE execute luuvcrm.api.get_menu_data
curl -sf https://$SITE/pos -o /dev/null && echo "pos ok"
```

**Rollback** is the same script with the previous tag. If the migrate changed the schema, the code alone is not enough — restore the backup as well.

**A new field or setting needs a patch.** `after_install` runs on a new site only, so a field added to `CUSTOM_FIELDS` or a key added to `settings.KEYS` reaches the live site through a numbered `patches.txt` entry and no other way. Adding one without its patch is a change that works everywhere except production.

**After the upgrade, check the scheduler is still alive** (`bench doctor`) and look at `/errors` — the front-end error log is where a broken deploy shows up first, and it stays quiet whether the app is healthy or the reporting is.

### 4.6 The print companion

`luuvcrm-print.apk` (70 MB) is an Android app that bridges the browser to an iMin thermal printer. There used to be two byte-identical copies; the `www/` one is deleted and `public/luuvcrm-print.apk` is served at `/assets/luuvcrm/luuvcrm-print.apk`. `pos.html` reads `localStorage['lf_paper_cols']` for the paper width (32 for 58 mm, 48 for 80 mm).

---

## 5. Where things live

`luuvcrm/api/` is a package — one module per domain. Add an endpoint to the
module it belongs to **and to `api/__init__.py`**; the package is the public
surface, and a name missing from `__init__.py` means `luuvcrm.api.<name>` stops
resolving, which shows up at the till as a button that 404s, not as an import
error. `test_api_package.py` catches it.

| Task | File |
|---|---|
| Take money — sale, payment, split | `luuvcrm/api/payments.py` |
| An order's life — send, amend, list, move | `luuvcrm/api/orders.py` |
| The till — shift, drawer, invoice plumbing | `luuvcrm/api/shifts.py` |
| What something costs | `luuvcrm/api/pricing.py` (`_resolve_item_rate`, `_service_charge_on`) |
| Change POS behaviour | `luuvcrm/www/pos.html` |
| Change the manager app | `luuvcrm/www/manager.html` + `manager.py`; numbers in `luuvcrm/api/reports.py` |
| Change purchasing / approvals | `luuvcrm/www/purchases.html`; `luuvcrm/api/purchases.py`, `approvals.py` |
| Change the kitchen display | `luuvcrm/www/kitchen.html` + `luuvcrm/api/kitchen.py` |
| Change receipt or KOT layout | `luuvcrm/api/printing.py`; `LFTemplate` in `pos.html` |
| Customer sign-in, PIN, OTP | `luuvcrm/api/customers.py` |
| Send a WhatsApp message | `luuvcrm/api/wa.py` — one seam, so a test can stub it once |
| Add a staff-action log entry | `luuvcrm.activity.log_activity` |
| Read front-end errors | `luuvcrm/api/errors.py`, reported by `www/lferr.js` |
| Who may do what | `luuvcrm/roles.py` |
| Change the order schema | `luuvcrm/zeloura/doctype/pos_order/pos_order.json` |
| Add a scheduled job | `hooks.py` → `scheduler_events` |

---

## 6. First week

1. Read `PRODUCTION_READINESS.md` end to end.
2. Get a local site running (§2) — the manual bootstrap will teach you the data model faster than reading will.
3. Place an order on `/pos`, watch it on `/kitchen`, pay it, print the receipt. Then find that flow in `api.py`.
4. Fix the four critical security findings. They are small, they are well-defined, and they are the fastest way to learn the request path.
5. Only then start on the schema-into-the-repo work. It is the largest job and everything else waits on it.
