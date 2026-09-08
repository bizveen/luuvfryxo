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
| **Touch anything in §4 before the §3 backup exists and is off the box** | Everything after §4 is irreversible in practice. A backup taken afterwards is a backup of the damage. This is the first write of the deploy and it is not optional, not even for "a small migrate". |
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

**Four things staff will notice on day one** — tell whoever is on shift:

- Waiter and Kitchen Staff lose their Frappe Desk login. The `/waiter` and
  `/kitchen` web apps are unaffected. If somebody genuinely used the Desk, give
  them Restaurant Manager instead.
- A Restaurant Manager can now open `/admin` and `/errors`. They were gated on
  `System Manager`, which the owner is deliberately not supposed to hold.
- **When the connection drops, the POS now shows a real error.** It used to
  answer a failed payment with a fabricated success and the words "Queued for
  sync", with no queue anywhere behind it. Staff will see failures they never
  saw — those failures were always happening.
- **The payment dialog can carry more than Cash, Card and Split.** Whatever the
  POS Profile lists is offered alongside them — PickMe, to start with. It is a
  §4 step, not automatic, and a cashier will see a new button the first time
  they open the till.

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

Read the live till's payment methods too — §4 has a manual step that depends on
what this returns:

```bash
bench --site <live> mariadb --execute \
  "select parent, idx, mode_of_payment from \`tabPOS Payment Method\`"
bench --site <live> mariadb --execute \
  "select parent, company, default_account from \`tabMode of Payment Account\`"
```

Write down which modes the profile lists and which ones have an account. A
profile carrying only `Cash` is the normal state on a site nobody ever filled
that table in on — it is not a fault, and it is not what the till has been
offering.

---

## 3. Back up first, then rehearse the migrate somewhere else

Never skip. `bench migrate` runs `remove_orphan_doctypes()`, which **deletes** a
DocType whose controller raises `ImportError`. That has already happened once on
this app: a title-case class name broke seven controllers and the next migrate
deleted all seven. The tables and rows survived; every field definition and
permission did not.

### 3a. Take the backup — the first thing you do

Before the recon if you like, but **never after anything in §4**.

```bash
bench --site <live> backup --with-files
# note the path it prints — you need it twice more
```

Then look at what it actually wrote, because a backup nobody checked is not a
backup:

```bash
ls -lh sites/<live>/private/backups/ | tail -5
```

A database dump of a few kilobytes is a failed backup that exited 0. Expect it
to be in the same order of magnitude as the last one.

**Copy it off the box.** A backup on the machine you are about to change is not
a backup of that machine.

If the deploy does not happen in this sitting, take a fresh one when you come
back. Restoring to yesterday means the restaurant re-rings yesterday's trade.

### 3b. Rehearse the migrate somewhere else

This also proves the backup restores — which is the only proof that matters,
and the reason it comes before the rehearsal rather than beside it:

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

**Stop if §3a did not happen.** Everything below writes to the live site. Say
out loud which backup file you are covered by and where the copy off the box
is; if you cannot name both, go back to §3a.

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
| `add_branch_field` | Adds the `restaurant` column to 7 doctypes (named below). **Adds only — every existing row stays NULL** |
| `grant_app_permissions` | The app's DocPerm table, and the revokes that close the REST-API walkaround |
| `sync_settings_fields` | Puts the settings keys on the Settings single |
| `narrow_desk_access` | Takes Desk access off Waiter and Kitchen Staff |
| `sync_custom_fields` | Applies the 32 declared custom fields |
| `add_menu_available_field` | Adds `Restaurant Menu Item.available`, default 1 |

### Which rows carry a restaurant, and what fills it in

`add_branch_field` puts a `restaurant` Link on seven doctypes — the ones that
represent a branch's own trade:

| DocType | What it is |
|---|---|
| `POS Order` | A ticket, from the till or a waiter |
| `Purchase Bill` | What a branch bought |
| `Expense` | Rent, salaries, utilities |
| `Wastage` | What was thrown away |
| `POS Approval` | A void, a discount, a bill sent up for a decision |
| `POS Cash Movement` | Money in or out of the drawer |
| `LF Activity Log` | The staff trail |

**A row created after this deploy fills itself in.** `branch.stamp` is a
`before_insert` hook on all seven: it writes `branch.current()` — the restaurant
the session belongs to — whenever the caller has not set one. The field is
`read_only`, so nobody types it and nobody can type the wrong one; it is set
once, at insert, from who was signed in.

That resolution is a chain, not a guess (`branch.current()`), each step a
stronger statement than the next:

1. A **User Permission on `Restaurant`** — Frappe's own answer to which branch
   a user belongs to, and the one that will drive the filtering when the reads
   flip to `get_list`.
2. The **open shift's POS Profile**. A cashier on a till is unambiguously at
   that till's branch.
3. A **POS Profile assigned to the user** — the same hop, for a manager who is
   not on a shift.
4. **The only Restaurant, if there is exactly one.** This is the step that
   carries a single-branch site: nothing has to be configured for the stamp to
   be right.

There is deliberately no "first of several" step. Where it cannot tell, it
returns **`None` rather than picking**, and the row is written unstamped — which
is why §2's `branch.check` matters before a second restaurant exists.

**Rows that already exist stay NULL.** Nothing on a one-restaurant site narrows
a read by branch — `sql_restaurant()` returns no condition at all unless the
session is scoped — so those rows stay visible exactly as they are today.
Filling them in is the backfill in §7, which is its own day and its own backup.
Do not run it here, and read §7 before turning on `apply_strict_user_permissions`:
that is the switch that makes an un-backfilled row disappear.

So after the deploy there is nothing to set by hand. The check is that the
stamping works, which §5 does with one real order.

---

### Payment methods — the one write this deploy does by hand

Not a patch, deliberately: it writes site data, and which methods a restaurant
takes is the restaurant's decision, not the release's.

The till shows **Cash, Card and Split whatever the profile says** — those three
are built into `pos.html` and were there long before the profile was read at
all. Anything beyond them — **PickMe**, and whatever comes after it — appears
only if the live POS Profile lists it.

So take the §2 output and compare:

- **`PickMe` missing from the profile** → the button will not be there. Add it.
- **`Credit Card` missing from the profile** → the button still works, because
  it is one of the built-in three. Add it anyway, so the card line has an
  opening float row at shift open and the profile says what the till actually
  takes.

```bash
bench --site <live> execute luuvcrm.setup_doctype.add_payment_mode
bench --site <live> execute luuvcrm.setup_doctype.add_payment_mode \
      --kwargs "{'mode': 'Credit Card'}"
```

Idempotent and additive — it creates the Mode of Payment if absent, books it an
account per company, and appends the row to every POS Profile. It never edits a
row that is already there, so running it twice is safe and running it for the
next aggregator is the same command with a different `mode`.

Three things have to line up, and each one fails somewhere different:

| Missing | Where it bites |
|---|---|
| The `Mode of Payment` | The button posts a mode that does not exist |
| An account for the company | The **POS Profile refuses to save**; and were the row already there, the first sale fails at submit — naming neither the mode nor the company |
| The row on the POS Profile | No button, and the shift close carries no float row for it |

**Read the account line it prints.** It prefers the company's default bank
account, then any bank account, and only then falls back to whatever `Cash` is
booked against — and it says which one it took. An aggregator settles days
later and its money is never in the drawer, so a fallback to the cash account
is a line to fix in ERPNext, not to skim past.

Then **`bench --site <live> clear-cache`** and reload `/pos` in the browser. The
list of methods is rendered into the page at load; a tablet with the till
already open keeps showing the old buttons until it is refreshed.

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
| Open the payment dialog | `Cash · Card · Split`, plus every extra method the live profile lists — `PickMe` among them if §4 added it |
| Take a payment | An invoice number comes back, and the receipt prints |
| The new order carries its branch | `bench --site <live> mariadb --execute "select name, restaurant from \`tabPOS Order\` order by creation desc limit 1"` — the restaurant is filled in, not NULL. If it is NULL, `branch.current()` resolved nothing: go back to §2's `branch.check` |
| Take one on `PickMe` | The invoice submits, and the shift close shows a `PickMe` row it filled in itself |
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
changed, so restore the backup as well. This is the moment §3a was for: a
restore takes the site back to the minute the backup was taken, and every sale
rung up since is gone with it, so decide quickly rather than deploying on top of
a symptom to save the last hour of trade.

```bash
bench --site <live> set-maintenance-mode on
cd apps/luuvcrm && git checkout <the commit main was on before the merge> && cd -
bench --site <live> restore <the §3a backup>
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
   across the seven doctypes named in §4. New rows have been filling themselves
   in since the deploy; this is only about the ones that predate it. Only
   meaningful after (1), and only ever with a fresh backup.
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
