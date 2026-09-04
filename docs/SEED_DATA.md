# Seed Data — what a fresh LuuvCRM site needs

**Status today:** there is no install-time seeding. `hooks.py` has no `after_install`, `patches.txt` is empty, and `add_seed_data.py` is a gitignored one-off script. Everything below either lives inside a `setup_*` HTTP endpoint that someone has to remember to call, or does not exist at all.

This document is the target: what a new site should have the moment `bench install-app luuvcrm` finishes.

---

## A. Defaults that already exist in code (move them to `after_install`)

These are written and sensible — they are just in the wrong place.

### Expense categories — `api.py:4196`
> Electricity · Water · Gas (LP) · Rent · Internet/Phone · Salaries/Wages · Repairs & Maintenance · Transport/Fuel · Cleaning & Consumables · Licences/Fees · Other

Seeded by `setup_expenses`, and correctly guarded: only seeds when the table is completely empty, so a manager who deletes one does not get it back. **Keep that behaviour** when moving it.

### Kitchen groups — `api.py:2631`
> Kottu · Rice & Noodles · Curries & Gravies · Biryani · Pizza · Beverages · Coffee & Tea · Desserts · Sides & Starters

Each with a sort order and a display colour. Sri Lanka-specific and good — but this is menu-shaped, not app-shaped. For a second customer it must be editable at setup, not hard-coded.

### Raw materials — `api.py:3744`
22 ingredients with UOM and a buying rate: tomatoes, lettuce, onion, potato, carrot, chicken, beef mince, fish, eggs, cheddar, milk, butter, burger buns, rice, flour, cooking oil, sugar, salt, LP gas, takeaway boxes, paper cups, napkins. Creates the `Raw Materials` item group and the UOMs `Kg`, `Litre`, `Dozen`, `Cylinder`, `Pack`.

**The rates are 2026 Sri Lankan prices for one supplier.** They are a demo convenience, not a default. Ship the item list and the UOMs; ship the prices as an *optional* demo fixture the installer opts into.

### Roles — `api.py:103`, `api.py:5816`, `activity.py:434`
- `POS_ROLES` = System Manager, POS User, Sales User, Cashier, Administrator
- `MENU_MANAGER_ROLES` / `MANAGER_ROLES` = System Manager, Administrator, Restaurant Manager
- `Waiter`, `Kitchen Staff` are referenced by the POS Order permissions

`Cashier`, `Waiter`, `Kitchen Staff`, `Restaurant Manager` are all created ad-hoc inside `setup_*` endpoints. They should be **role fixtures** in the repo, with their DocPerms declared, not granted at runtime by `add_permission()` calls (see `setup_pos_shift_perms`).

### Guide cashback default — `api.py:6026`
`GUIDE_DEFAULT_PERCENT = 5.0`. Belongs in a `Guide Settings` single, with 5.0 as the default value.

### Demo menu — `luuvcrm/add_seed_data.py` (gitignored)
5 tables, 10 food items, 5 beverages, with LKR prices. Useful for a demo site. Should become `luuvcrm/demo/` behind an explicit `bench execute luuvcrm.demo.install` — never automatic.

---

## B. What is missing and must be added

### B1 · A settings single — the biggest gap

There is currently **no** place to configure an installation. The brand name is hard-coded in 25 HTML files ("Luuv Fryxo"), the manager PIN is a literal `"4321"` in `www/manager.py:24`, and the app reads `frappe.get_value("Restaurant", {}, "name")` — *the first restaurant record, whichever that happens to be* (`api.py:13`).

Create **`LuuvCRM Settings`** (single) with:

| Field | Default | Used by |
|---|---|---|
| `brand_name` | site name | every page title and header |
| `brand_logo` | — | POS, receipts, PWA manifests |
| `primary_colour` | `#10B981` | page CSS variables |
| `currency` | `LKR` | all money formatting |
| `active_restaurant` | first `Restaurant` | replaces the `{}` lookup at `api.py:13` |
| `service_charge_percent` | `10` | `_service_charge_on` (today reads `Restaurant.service_charge`) |
| `service_charge_on_takeaway` | `0` | current behaviour, currently implicit |
| `manager_pin` | *(hashed, blank)* | replaces the literal `"4321"` |
| `pos_price_list` | `Standard Selling` | `_get_pos_price_list` |
| `purchase_price_list` | `Standard Buying` | purchases module |
| `whatsapp_number` | — | replaces `wa.me/9470XXXXXXX` in `www/menu.html:225` |
| `whatsapp_enabled` | `0` | OTP + order confirmation |
| `loyalty_enabled` | `0` | loyalty endpoints |
| `guides_enabled` | `0` | tour-guide module |
| `kiosk_enabled` | `0` | `/kiosk` |
| `approval_threshold` | `0` | POS approvals |
| `activity_retention_days` | `90` | today hard-coded in the purge |

Every feature flag defaulting to **off** matters: a new restaurant should not inherit Luuv Fryxo's WhatsApp promos and tour-guide cashback on day one.

### B2 · POS Profile and payment modes

`place_order` calls `_active_pos_profile_name()` and `_ensure_open_shift()`. With no POS Profile the first sale throws. Seed one:

- **POS Profile** `Default` — company from `Global Defaults`, warehouse `Stores`, currency from settings
- **Modes of Payment**: `Cash`, `Card`, `Bank Transfer` — plus the `Cash+Card` split the POS offers
- Attach all three to the profile as `POS Payment Method` rows

### B3 · Price lists

`Standard Selling` and `Standard Buying` come from ERPNext, but the app assumes both exist and are enabled. Assert that in `after_install` and fail loudly with a clear message rather than returning a silent `0` rate.

### B4 · The `Restaurant` record

Supplied by the `hospitality` app, but LuuvCRM cannot work without exactly one. Create a placeholder named from the site, with `service_charge` from settings, and at least one `Restaurant Table` so the table view is not empty.

### B5 · Custom fields as patches

~24 `create_custom_field` calls currently live inside `setup_*` endpoints — `takeaway`, `sent_qty`, `paid_qty`, `notes`, `scheduled_time`, `kitchen_status` options, the invoice link, the check-printed flag, the approval fields. Every one should be a numbered entry in `patches.txt` so `bench migrate` applies them and a second site cannot drift.

### B6 · Item groups

`Food`, `Beverage`, `Raw Materials` are created in three different places. Declare them once as a fixture.

---

## C. What must **not** be seeded

- **Raw material prices.** Supplier- and date-specific.
- **The Luuv Fryxo menu.** Demo only, opt-in.
- **The kitchen group list**, as-is. Offer it as a "Sri Lankan restaurant" preset the installer can pick, alongside an empty option.
- **Any WhatsApp credential, any real phone number, any manager PIN.**
- **Tour guides, promo campaigns, loyalty rules.** Off by default; the customer switches them on.

---

## D. Target install flow

```bash
bench get-app luuvcrm
bench --site <site> install-app luuvcrm     # pulls erpnext + hospitality
```

`after_install` then, in order:

1. Verify erpnext and hospitality are installed — throw a readable error if not.
2. Create roles + DocPerms (fixtures).
3. Create item groups and UOMs.
4. Create `LuuvCRM Settings` with the defaults in B1, features off.
5. Create the POS Profile, modes of payment, price-list checks.
6. Create the placeholder `Restaurant` + one table.
7. Seed expense categories (empty-table guard).
8. Seed raw-material *items* without prices.
9. Print a next-steps message: set the brand, the manager PIN, the WhatsApp number, import the menu.

Optional, explicit:

```bash
bench --site <site> execute luuvcrm.demo.install   # demo menu, tables, prices
```

**Definition of done:** a brand-new site reaches a working POS sale with no HTTP `setup_*` call and no manual Desk editing. Until that passes, the app is not installable — see `PRODUCTION_READINESS.md` §B1.
