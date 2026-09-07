# Deploy runbook — `product` → `main` → the live restaurant

**Who this is for:** the agent doing the deploy. Read all of it before running
anything. It is written to be followed in order, and every phase ends with a
check you can actually run.

**What this deploy is.** `product` is 55 commits ahead of `main`: the schema
moves into the repo, `api.py` becomes a package, and about ten things that are
silently broken on the live site start working. `main` is the live restaurant.

**The two sentences that matter most:**

1. **No live row is rewritten by this deploy.** Everything here is additive —
   new columns, new DocType definitions, new permissions. The data migrations
   that *would* rewrite rows are listed in §7 and are explicitly **not** part of
   this deploy.
2. **The one irreversible step is the DocType conversion** (§4). It is fine, and
   it is the point of the whole release — but it is only fine after §2 and §3.
   Do not reorder those.

---

## 0. Hard rules

Break any of these and stop; ask a human.

| Never | Why |
|---|---|
| Deploy without running §2 (drift report) and §3 (rehearsal) | The DocType sync is the irreversible step. §2 turns an assumption into a fact; §3 is the only thing that catches `remove_orphan_doctypes` deleting a DocType. |
| Run `branch.backfill()` | It writes a `restaurant` value onto every historical row across seven doctypes. It is a data migration, not a deploy. See §7. |
| Turn on `apply_strict_user_permissions` | Every row whose `restaurant` is still empty disappears from every non-Owner at once — which today is every row. See §7. |
| Run this branch's tests against the live site | `run-tests` creates and deletes real records. Tests belong on `test.localhost` or a scratch site, never on production. |
| Run `main`'s tests against a site `product` has touched | `main` still builds its schema at runtime, so its `ensure_setup()` recreates DocTypes as `custom: 1` and undoes the conversion. |
| `bench --site <live> execute luuvcrm.setup_doctype.execute` | That is the fresh-install provisioner. On a live site the patches do the work; running it by hand is how you get a surprise. |
| Delete anything to "clean up" | Nothing in this release requires a delete. An Item, a User or a DocType that looks unused is linked to invoices. |
| Force-push, `git reset --hard`, or resolve a merge conflict by taking one side wholesale | If the merge conflicts, stop. `product` was built to merge cleanly; a conflict means something changed on `main` that nobody accounted for. |

**Reading a result:** never read a test or migrate result through `tail` or a
pipe — the summary prints before the drift report, so the tail shows a green
line from a red run, and the pipe makes the exit code the tail's. Redirect to a
file and grep it:

```bash
bench --site <site> migrate > /tmp/migrate.log 2>&1; echo "exit=$?"
grep -nEi "^Error|Traceback|does not exist" /tmp/migrate.log
```

---

## 1. What you are deploying

Facts, so you can recognise a wrong turn.

| | `main` (live now) | `product` (deploying) |
|---|---|---|
| API | one `api.py`, ~7,270 lines | `luuvcrm/api/`, 26 modules |
| Whitelisted endpoints | 153 | 182 — **every one of main's still resolves** |
| Schema | built at runtime, `custom: 1` | 26 DocTypes as JSON in the repo, `custom: 0` |
| `patches.txt` | empty | 8 patches |
| Custom fields | created by `setup_*` endpoints | 32 declared, applied by a patch |
| Tests | 6 files | 28 files, 793 test methods |

**Ten things that are broken on live today and start working:** `/order` is a
500 for customers; `/home` serves ERPNext's page instead of the restaurant's;
`/` is a 404; `/activity` is a 500; the manager's "Add item" button fails; the
service worker caches nothing; the money endpoints write nothing to the staff
trail (so no flag rule can ever fire); the front-end error log posts to an
endpoint that does not exist; `/admin` and `/errors` are shut to the owner; and
nine security findings are closed.

**Three things staff will notice on day one** — tell whoever is on shift:

- Waiter and Kitchen Staff lose their Frappe Desk login. The `/waiter` and
  `/kitchen` web apps are unaffected. If somebody genuinely used the Desk, give
  them Restaurant Manager instead.
- A Restaurant Manager can now open `/admin` and `/errors`. They were gated on
  `System Manager`, which the owner is deliberately not supposed to hold.
- **When the connection drops, the POS now shows a real error.** It used to
  answer a failed payment with a fabricated success and the words "Queued for
  sync", with no queue anywhere behind it. Staff will see failures they never
  saw — those failures were always happening.

---

## 2. Reconnaissance — read-only, changes nothing

Run these against the **live** site. None of them writes.

```bash
bench --site <live> execute luuvcrm.setup_doctype.report_drift
```

This is the step that turns "the JSON should match live" into a fact. It reports
missing DocTypes, missing custom fields and absent parents.

- **`{"doctypes": [], "custom_fields": [], ...}`** → proceed.
- **Anything listed** → stop. Post the output. A DocType or field the live site
  has and the repo does not is exactly what the JSON sync would overwrite.

Then record the starting state, so §5 has something to compare against:

```bash
bench --site <live> execute luuvcrm.branch.check          # branch wiring
bench --site <live> execute luuvcrm.roles.desk_access_drift
bench --site <live> version
bench --site <live> doctor                                # is the scheduler alive?
```

Save all four outputs. Note the **POS Profile name**, the **Restaurant name**,
and roughly how many `POS Order` rows exist — you will want them in §5.

---

## 3. Backup, and rehearse the migrate somewhere else

Never skip. `bench migrate` runs `remove_orphan_doctypes()`, which **deletes** a
DocType whose controller raises `ImportError`. That has already happened once on
this app: a title-case class name broke seven controllers and the next migrate
deleted all seven. The tables and rows survived; every field definition and
permission did not.

```bash
bench --site <live> backup --with-files
# note the path it prints — you need it twice more
```

Copy the backup off the box. Then rehearse:

```bash
bench new-site rehearse.localhost --db-name rehearse --admin-password <pw>
bench --site rehearse.localhost restore <the backup you just made>
cd apps/luuvcrm && git checkout product && cd -
bench --site rehearse.localhost migrate > /tmp/rehearse.log 2>&1; echo "exit=$?"
grep -nEi "^Error|Traceback|orphan|deleted" /tmp/rehearse.log
bench --site rehearse.localhost execute luuvcrm.setup_doctype.report_drift
```

**Pass condition:** migrate exits 0, the log names no deleted DocType, and the
drift report is empty. If any of those fails, **stop** — the same thing will
happen to the live site.

Also back up the encryption key **separately from the database**. It is not in
the dump, and without it a restored site cannot read a single WhatsApp token or
verify a `/track` link:

```bash
grep encryption_key sites/<live>/site_config.json
```

---

## 4. The deploy

The live host is a non-bare checkout reached over an SMB share. Mount it first —
if the share is not mounted, git reports "does not appear to be a git
repository", which is a mount problem, not an auth problem.

```bash
gio mount -a "smb://192.168.1.8/users"
```

The host's working tree routinely carries whitespace-only diffs (last-line
CRLF/LF) in `www/{kiosk,my_orders,myorders,qrcodes,track}.html` and
`www/qrcodes.py`. A dirty tree rejects the push, so stash them **on the host**
first. Then, on the development machine:

```bash
git checkout main
git merge product              # expect a clean merge; a conflict means STOP
git push host main
```

On the host, with the restaurant closed or quiet:

```bash
bench --site <live> set-maintenance-mode on
bench --site <live> migrate > /tmp/migrate.log 2>&1; echo "exit=$?"
grep -nEi "^Error|Traceback|does not exist" /tmp/migrate.log
bench build --app luuvcrm
bench --site <live> clear-cache
bench --site <live> set-maintenance-mode off
bench restart
```

`bench build` is not optional — the front-end assets changed.

**Eight patches run during that migrate.** They are all additive:

| Patch | What it does |
|---|---|
| `create_roles` | Creates any missing app role; re-enables a disabled one |
| `grant_pos_permissions` | The DocPerms a till needs on ERPNext's own doctypes |
| `add_branch_field` | Adds the `restaurant` column to 7 doctypes. **Adds only — every existing row stays NULL** |
| `grant_app_permissions` | The app's DocPerm table, and the revokes that close the REST-API walkaround |
| `sync_settings_fields` | Puts the settings keys on the Settings single |
| `narrow_desk_access` | Takes Desk access off Waiter and Kitchen Staff |
| `sync_custom_fields` | Applies the 32 declared custom fields |
| `add_menu_available_field` | Adds `Restaurant Menu Item.available`, default 1 |

---

## 5. Verify — in this order

Schema first, then a page, then the till. Stop at the first failure.

```bash
bench --site <live> execute luuvcrm.setup_doctype.report_drift    # must be empty
bench --site <live> execute luuvcrm.api.get_menu_data             # must list items
bench --site <live> doctor                                        # scheduler alive
```

Then in a browser, signed in as a real staff account:

| Check | Expect |
|---|---|
| `/pos` opens | The menu grid and the table list, both populated |
| Open a shift | It opens and reports a float |
| Build a bill, send to kitchen | The ticket appears on `/kitchen` |
| Take a payment | An invoice number comes back, and the receipt prints |
| Print a KOT | The right sections on the paper |
| `/waiter`, `/kitchen` | Load, and the menu is not empty |
| `/manager` | Today's numbers, and the approvals list |
| `/` and `/home` | The restaurant's landing page — **not** ERPNext's |
| `/order`, `/menu`, `/kiosk`, `/track`, `/cus` | All load |
| `/admin` | The portal, with the sections down the side |
| `/errors` | Loads. New rows here are **not** new bugs — the reporter was posting into a void before today |

**An empty menu on `/pos` is the one symptom to take seriously**: it means the
menu read is not resolving a restaurant, and every ordering screen is affected.
Go to §6.

---

## 6. If something is wrong

**A screen is blank or refuses.** Most likely the DocPerm revokes: a Cashier no
longer has direct `write`/`delete` on `POS Approval`, `Purchase Bill`,
`Expense`, `Wastage`, `POS Cash Movement`, `Tour Guide` and
`Guide Cashback Txn`. Every app endpoint writes those through
`ignore_permissions`, so the till itself is unaffected — but if a screen went
blank, this is the first place to look:

```bash
bench --site <live> execute luuvcrm.roles.missing_docperms
bench --site <live> execute luuvcrm.roles.walkable_docperms
```

**The till cannot take money.** Re-grant the ERPNext DocPerms and clear the
cache; this is idempotent:

```bash
bench --site <live> execute luuvcrm.roles.ensure_pos_docperms
bench --site <live> clear-cache && bench restart
```

**Anything worse — roll back.** The code alone is not enough if the schema
changed, so restore the backup as well:

```bash
bench --site <live> set-maintenance-mode on
cd apps/luuvcrm && git checkout <the commit main was on before the merge> && cd -
bench --site <live> restore <the §3 backup>
bench --site <live> migrate
bench --site <live> set-maintenance-mode off && bench restart
```

Rolling back is not a failure. Deploying on top of a symptom nobody understands
is.

---

## 7. What this deploy deliberately does **not** do

These three are real work, they touch live data, and each deserves its own day
and its own backup. **None of them belongs in this deploy.**

1. **Backfill `cost_center` on existing `POS Invoice` rows.** Every one is empty
   today, because the POS Profile never carried a cost centre. Until they are
   filled, historical revenue belongs to no branch — the report helpers degrade
   to unscoped, so nothing breaks.
2. **`branch.backfill()`** — stamps the restaurant onto every historical row
   across seven doctypes. Only meaningful after (1), and only ever with a fresh
   backup.
3. **`apply_strict_user_permissions`** — leave it **off**. Turning it on flips
   the `ifnull` branch that keeps NULL rows visible, and every un-backfilled row
   vanishes for every non-Owner at once.

Order, when the day comes: add the field → backfill → verify → *then* consider
turning strict permissions on. Never create a second Restaurant before the
backfill.

---

## 8. The week after

- **Re-point the counter tablets** from `/pos2` to `/pos`. The redirect is there
  for one release and then goes.
- **Fill in `LuuvCRM Settings`** if anything should differ from the defaults —
  though the defaults are already this restaurant's own name, town, phone and
  colour, so nothing looks different until somebody changes it.
  `bench --site <live> execute luuvcrm.settings.describe` prints every key,
  where it lives, and whether it is still on its default.
- **Watch `/errors` and `/activity`.** Both are working for the first time. The
  first week's rows are a backlog, not a regression.
- **Confirm gzip is on in nginx.** Measured on this app: all 24 pages are 766 KB
  raw and 184 KB compressed. It is one config line.

  ```bash
  curl -sI -H "Accept-Encoding: gzip" https://<live>/pos | grep -i content-encoding
  ```

- **A new field or setting needs a numbered patch.** `after_install` runs on a
  new site only, so a field added to `CUSTOM_FIELDS` or a key added to
  `settings.KEYS` reaches the live site through `patches.txt` and no other way.
  Adding one without its patch works on every developer's machine and reaches
  production never.
