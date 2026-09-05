# LuuvCRM — Developer Guide

How to set up, develop, and deploy this app. Written for a developer who has never seen it.

> **Read this first:** the app does not currently install cleanly on a fresh site — most of the schema is created at runtime by HTTP endpoints rather than shipped in the repo. §2 tells you how to work around that today; `PRODUCTION_READINESS.md` §B1 tells you how to fix it properly.

---

## 1. What you are working with

A Frappe v15 app that turns ERPNext + Hospitality into a restaurant operating system: point of sale, waiter and kitchen apps, a self-order kiosk, a customer PWA, purchasing with approvals, a manager phone dashboard, loyalty, WhatsApp ordering and a tour-guide commission scheme.

**Architecture in one paragraph.** There is no front-end framework and no build step. Every screen is a single hand-written HTML file under `luuvcrm/www/` with inline CSS and JavaScript, paired with a `.py` context builder that Frappe's website router calls. Those pages talk to the backend over `fetch` to whitelisted Python methods, almost all of which live in one file, `luuvcrm/api.py` (7,095 lines). Orders are held as a custom `POS Order` doctype while in progress, then materialised into an ERPNext `POS Invoice` on payment. Vanilla, boring, and easy to debug — with the trade-off that `api.py` is very large and the pages are unminified.

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

- **`/pos` is the POS.** There is one. There used to be three (`/pos`, `/pos2` and the `/pos3` staging copy); pos3 had all the work, so it became `/pos` and the other two were deleted. Read the header comment at the top of `www/pos.html` before editing it — the tablet layer, the printer bridge and the Google-Docs print layouts are all documented there.
- **`/myorders` and `/my_orders` are two live copies of the same screen.** So are `/customer` and `/cus`. Check which one the client is actually using before you fix a bug in one.
- **The brand is hard-coded.** "Luuv Fryxo" appears in every page; `hooks.py` says "Zeloura"; the app is `luuvcrm`. Same product, three names.
- **`zeloura_api.py` and `zeloura/` clothing code** (`Zeloura Theme`, `Zeloura Product`, `get_landing`) are from a different product. Confirm with the owner before removing.
- **`check_*.py`, `debug_*.py`, `fix_*.py`** are gitignored one-off scripts run against the live DB. Not part of the app.

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

There are none. `luuvcrm/test_child_parent.py` is a scratch script, not a test. If you add anything to the money path, add the test with it:

```bash
bench --site dev.localhost run-tests --app luuvcrm
```

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
- [ ] `encryption_key` set and backed up **separately from the database** (WhatsApp tokens are encrypted with it; lose it and they are gone)
- [ ] Automated daily backups, restored to a scratch site at least once to prove they work
- [ ] Off-site backup copies
- [ ] Supervisor + nginx via `bench setup production <user>`
- [ ] Log rotation
- [ ] A scheduler that actually runs — `bench doctor` (the activity purge depends on it)
- [ ] Firewall: 80/443 only; MariaDB and Redis not exposed
- [ ] Uptime and disk monitoring
- [ ] The security fixes in `PRODUCTION_READINESS.md` §3 — **`update_order`, client-supplied prices, and `customer_get_orders` are internet-reachable today**

### 4.4 The print companion

`luuvcrm-print.apk` (70 MB) is an Android app that bridges the browser to an iMin thermal printer. Two byte-identical copies exist, in `public/` and `www/` — serve the `public/` one and delete the other. `pos.html` reads `localStorage['lf_paper_cols']` for the paper width (32 for 58 mm, 48 for 80 mm).

---

## 5. Where things live

| Task | File |
|---|---|
| Add a POS endpoint | `luuvcrm/api.py` |
| Change POS behaviour | `luuvcrm/www/pos.html` |
| Change the manager app | `luuvcrm/www/manager.html` + `manager.py` |
| Change purchasing / approvals | `luuvcrm/www/purchases.html` + `api.py` purchases section |
| Change the kitchen display | `luuvcrm/www/kitchen.html` |
| Change receipt or KOT layout | `get_receipt_print_data` / `get_kot_print_data` in `api.py`; `LFTemplate` in `pos.html` |
| Add a staff-action log entry | `luuvcrm.activity.log_activity` |
| Change the order schema | `luuvcrm/zeloura/doctype/pos_order/pos_order.json` |
| Add a scheduled job | `hooks.py` → `scheduler_events` |
| Price resolution | `_resolve_item_rate`, `api.py:70` |
| Service charge | `_service_charge_on`, `api.py` |

---

## 6. First week

1. Read `PRODUCTION_READINESS.md` end to end.
2. Get a local site running (§2) — the manual bootstrap will teach you the data model faster than reading will.
3. Place an order on `/pos`, watch it on `/kitchen`, pay it, print the receipt. Then find that flow in `api.py`.
4. Fix the four critical security findings. They are small, they are well-defined, and they are the fastest way to learn the request path.
5. Only then start on the schema-into-the-repo work. It is the largest job and everything else waits on it.
