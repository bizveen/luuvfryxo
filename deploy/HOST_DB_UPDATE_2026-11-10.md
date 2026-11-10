# Host DB update — Kurunegala menu descriptions

Date: 2026-11-10
Target site: `fryxo`
Restaurant: `Kurunegala`

This is a database-only menu update. Do not edit the Canva design.

## Required changes

- Remove `KG-EXTRA-SALSA` (Tomato Salsa) from the active Kurunegala menu's
  `Add-ons` category. Keep the Item itself for historical order references.
- Tomato Salsa is now included by default with every Loaded Fries item.
- Update the six Loaded Fries Item descriptions so the customer detail view
  detects and displays Tomato Salsa under **Sauces included**.

## Apply on the host

Run from `/home/frappe/frappe-bench` in the backend container:

```bash
bench --site fryxo execute luuvcrm.api.menu.update_menu_item --kwargs "{'item_code':'KG-FRIES-CHICKEN','description':'Crispy chicken, creamy coleslaw, mozzarella cheese, Tomato Salsa, Luuv Chef special Hot Sauce, White Sauce, Golden Sauce and Smoky Brown Sauce.','restaurant':'Kurunegala'}"
bench --site fryxo execute luuvcrm.api.menu.update_menu_item --kwargs "{'item_code':'KG-FRIES-FISH','description':'Crispy fish, creamy coleslaw, mozzarella cheese, Tomato Salsa, Luuv Chef special Hot Sauce, White Sauce, Golden Sauce and Smoky Brown Sauce.','restaurant':'Kurunegala'}"
bench --site fryxo execute luuvcrm.api.menu.update_menu_item --kwargs "{'item_code':'KG-FRIES-PRAWN','description':'Tempura prawns, creamy coleslaw, mozzarella cheese, Tomato Salsa, Luuv Chef special Hot Sauce, Smoky Brown Sauce, White Sauce and Golden Sauce.','restaurant':'Kurunegala'}"
bench --site fryxo execute luuvcrm.api.menu.update_menu_item --kwargs "{'item_code':'KG-FRIES-BEEF','description':'Spicy masala ground beef, garden mayo salad, mozzarella cheese, Tomato Salsa, Luuv Chef special Hot Sauce, Smoky Brown Sauce, White Sauce and Golden Sauce.','restaurant':'Kurunegala'}"
bench --site fryxo execute luuvcrm.api.menu.update_menu_item --kwargs "{'item_code':'KG-FRIES-SAUSAGE','description':'Grilled bratwurst sausage, creamy salad, mozzarella cheese, Tomato Salsa, Smoky Brown Sauce, Golden Sauce, White Sauce and Luuv Chef special Hot Sauce.','restaurant':'Kurunegala'}"
bench --site fryxo execute luuvcrm.api.menu.update_menu_item --kwargs "{'item_code':'KG-FRIES-MIX','description':'Crispy chicken, prawns and sausage with golden fries, creamy coleslaw, mozzarella cheese, Tomato Salsa and signature sauces.','restaurant':'Kurunegala'}"
bench --site fryxo execute luuvcrm.api.menu.remove_menu_item --kwargs "{'item_code':'KG-EXTRA-SALSA','restaurant':'Kurunegala'}"
bench --site fryxo clear-cache
bench --site fryxo clear-website-cache
```

## Verify

```bash
bench --site fryxo mariadb --execute "SELECT name,item_name,description FROM tabItem WHERE name LIKE 'KG-FRIES-%' ORDER BY name;"
bench --site fryxo mariadb --execute "SELECT parent,item,rate,available FROM \`tabRestaurant Menu Item\` WHERE item='KG-EXTRA-SALSA';"
```

Expected:

- All six Loaded Fries descriptions contain `Tomato Salsa`.
- The second query returns no active-menu row.
- `KG-EXTRA-SALSA` still exists in `tabItem`; only its active-menu row is removed.

## Rollback

Restore Tomato Salsa to the active menu at Rs 100:

```bash
bench --site fryxo execute luuvcrm.api.menu.restore_menu_item --kwargs "{'item_code':'KG-EXTRA-SALSA','rate':100,'restaurant':'Kurunegala'}"
bench --site fryxo clear-cache
bench --site fryxo clear-website-cache
```

If the description changes also need to be rolled back, use the descriptions
from the pre-change database backup or the previous approved menu revision.

## Customer OTP — production follow-up

Full secure setup and verification instructions are in
[`TEXTLK_SMS_HOST_SETUP.md`](TEXTLK_SMS_HOST_SETUP.md).

The customer registration flow now sends its one-time verification code through
Text.lk SMS only. Existing customer accounts continue to sign in with their PIN
and are not asked for OTP again.

Before deploying this code to the host, configure the production site with the
real API token and an approved Text.lk Sender ID:

```bash
bench --site fryxo set-config luuvcrm_textlk_api_token '<TEXTLK_API_TOKEN>'
bench --site fryxo set-config luuvcrm_textlk_sender_id 'LUUV FRYXO'
bench --site fryxo set-config luuvcrm_textlk_allow_demo 0
bench --site fryxo clear-cache
```

Do not use `TextLKDemo` or enable `luuvcrm_textlk_allow_demo` on the production
host. Those settings are only active on `test.localhost` for device testing.
