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
| **Forget that `luuvcrm` is installed on a second site** | The bench has ~15 sites on one backend, and this app is installed on `clothing` (the Zeloura shop) as well as on `fryxo`. Its DocTypes live in module **Zeloura**, which this app owns, and its theme pages call `luuvcrm.zeloura_api.get_csrf`. Hooks are bench-wide. Before the deploy, that site gets `luuvcrm_passive: 1` in its site_config (the script does it), and `live-only/zeloura/` goes back over the package after every overlay. Never `bench migrate` the clothing site with this app's JSON in place unless you mean to create 24 restaurant DocTypes there. |
| **Deploy by replacing the app directory** | The live directory is not a git checkout (1 commit, 54 dirty entries), not a volume (it lives in the backend container's writable layer — as does a separate copy in each worker), and not only ours (the clothing code, `.bak` files, scratch scripts, two APKs). The deploy is an overlay + the paths in `deploy/REMOVE.list`, and nothing else moves. See §4. |
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

**Checked on the live site on 2026-09-08/09, read-only:** `/`, `/home` and `/order` all answer 200 there
(luuvcrm was installed after erpnext on both sites, so its pages already win), Website Settings already
points `/` at `home`, the live `api.py` has every endpoint this branch has, and there are no hand edits on the
host that the repo lacks — except the clothing site's code, which is now `live-only/`. The list below is what
the *code* fixes; the first three items were true of the dev bench, not of the restaurant.

**Things that are broken and start working:** `/order` is a
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
and the reason it comes before the rehearsal rather than beside it.

On this host the "somewhere else" is a **throwaway container from the same
image**, sharing the sites volume and the database server but holding its own
copy of the app directory — so the live backend keeps running the old code
while the new code migrates a restored copy of the site. The script does all
of it and removes both the container and the site afterwards:

```bash
export DB_ROOT_PASSWORD=<mariadb root>          # new-site / restore / drop-site need it
deploy/vps-deploy.sh rehearse                    # add RUN_TESTS=1 to run the 793 tests on the throwaway site
```

What it does, so you can recognise a wrong turn: `docker run` the backend's
image with the three `frappe_docker_*` volumes → overlay the tarball into
*that* container → `bench new-site rehearse.localhost` → `restore` the §3a
dump → `migrate` to a log → `report_drift`, `branch.check`,
`settings.describe`, the menu → `bench serve` on port 8001 inside the
container and fetch `/`, `/home`, `/order`, `/menu`, `/pos-login` with a
`Host:` header → `drop-site` → `docker rm`. It ends with `REHEARSAL PASS
<sha>` or it fails, and `deploy` refuses to run without that line for the
same commit.

**Pass condition:** migrate exits 0, the log names no deleted DocType, the
drift report is empty, every public page renders. If any of those fails,
**stop** — the same thing will happen to the live site.

Also back up the encryption key **separately from the database**. It is not in
the dump, and without it a restored site cannot read a single WhatsApp token or
verify a `/track` link. `bench backup` writes it into
`*-site_config_backup.json`, which `backup` copies to the PC and checks for.

---

## 4. The deploy

**Stop if §3a did not happen.** Everything below writes to the live site. Say
out loud which backup file you are covered by and where the copy off the box
is; if you cannot name both, go back to §3a.

There is no `git push` to the host. The app directory there is not a
checkout, not a volume, and not only ours — so the deploy is an **overlay**:

```bash
deploy/vps-deploy.sh preflight      # read-only. Lists CHANGED / NEW / REMOVE / KEPT for every live file; builds the tarball
deploy/vps-deploy.sh backup         # §3a, copied to deploy-state/backups/<ts>/ on the PC
deploy/vps-deploy.sh rehearse       # §3b
deploy/vps-deploy.sh deploy         # below. Refuses without a backup < 12 h old and a passed rehearsal for this commit
```

`deploy` does, in order, with the restaurant closed or quiet:

| Step | What | Why |
|---|---|---|
| 0 | `bench --site clothing set-config luuvcrm_passive 1` | The one write to the other site. Turns this app's request hooks and patches off there. |
| 1 | `set-maintenance-mode on` for `fryxo` | Only that site; the other 14 keep serving. |
| 2 | tar of the live app dir → host | The rollback source, taken *after* maintenance mode so nothing is mid-write. |
| 3 | overlay into the backend | `git archive` of HEAD copied over the tree; the paths in `deploy/REMOVE.list` moved to `apps/luuvcrm.removed-<ts>/`; `live-only/zeloura/luuvcrm/` copied back over the package; `chown 1001`; every module compiled. |
| 4 | the same overlay into the scheduler and the two queue workers | Each has its **own** copy of the app in its own container layer. The doc_events and the daily purge run there. |
| 5 | `docker restart` the backend | gunicorn runs with `--preload`, so nothing short of a restart loads new Python. ~15 s blip for every tenant. Readiness is probed through the *clothing* site's `/api/method/ping`, because fryxo is in maintenance mode. |
| 6 | `bench --site fryxo migrate` → `deploy-state/migrate-<sha>-<ts>.log` | The irreversible step. A non-zero exit stops the script with the site still in maintenance mode. |
| 7 | `sites/assets/luuvcrm`: replace the symlink with a copy of `public/` | The assets volume is shared with the nginx frontends, but the symlink pointed at `apps/…/public`, which resolves to *each frontend's own image copy* — so the new `app/icon-*.png` would have 404'd. |
| 8 | `clear-cache`, `clear-website-cache`, maintenance off | |
| 9 | restart the workers | |

Then `smoke` (§5), automatically. No `bench build`: this app has no JS
bundles, only `public/css`, and step 7 carries the assets.

**What the overlay leaves alone**, on purpose: every `.bak`, the `check_*` /
`debug_*` / `fix_*` scratch scripts, `wwwpos.py`, `wwwwaiter.py`, the two
APKs, `www/app.*.disabled`, and the clothing site's code. `preflight` prints
them as `KEPT`. Only `deploy/REMOVE.list` moves anything, and it moves it
aside rather than deleting it.

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

### Bank Draft comes off the two tills that list it

`deploy` runs `remove_payment_mode("Bank Draft")` after migrate (step 7b,
`DROP_MODES`). kandy and Kurunegala carry a `Bank Draft` row booked to
`Cash - LF` that no cashier has ever seen — the old POS ignored the profile.
The new one shows every listed mode as a button, so without this a cashier
who has never seen "Bank Draft" gets a button that behaves like Cash. The
Mode of Payment and every invoice that used it stay; `add_payment_mode
--kwargs "{'mode': 'Bank Draft'}"` puts it back. Set `DROP_MODES=""` to keep it.

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

**Anything worse — roll back.**

```bash
deploy/vps-deploy.sh rollback          # the pre-deploy app dir back into the backend and the workers, restart, caches
deploy/vps-deploy.sh rollback --db     # the same, plus `bench restore` of the §3a dump (needs DB_ROOT_PASSWORD)
```

The code alone is enough **only if migrate did not run** — the script stops
before it on a failed overlay. Once migrate has run, the DocType conversion is
in the database and the old code cannot read it back: use `--db`. A restore
takes the site back to the minute the backup was taken, and every sale rung
up since is gone with it, so decide quickly rather than deploying on top of a
symptom to save the last hour of trade. The failed tree stays on the host as
`apps/luuvcrm.failed-<ts>` for the post-mortem.

Rolling back is not a failure. Deploying on top of a symptom nobody understands
is.

---

## 7. What is still open after the deploy (2026-09-10)

The deploy went live on 2026-09-10 (`8582a45`, then `fc19acd` the same
morning). No complaint from the floor on day one. Everything below was
found while getting it there and is **not done**. Each line says what, why
it matters, and what the fix is, so the next session can pick any of them
up without re-discovering it.

### 7a. Security — do these first

| # | What | Why it matters | Fix |
|---|---|---|---|
| 1 | **`Administrator` still logs in with the password `admin`** on the public site. | Anyone who has read the old SKILL.md, or guesses, owns every one of the ~15 tenants on this backend — the Administrator account is bench-wide. | Change it in Desk (`/app/user/Administrator` → Reset Password), and store the new one in a password manager, not in a repo file. |
| 2 | **Three test accounts are enabled as System Manager**: `test@example.com`, `testdelete@example.com`, `testpassword@example.com` (plus `test1`–`test4`, `testperm`, `test'5@` as enabled users). | Leftovers of somebody's test run on the live site. Each is a login with full access and a guessable name. | Disable them (User → Enabled off). Do not delete: an Activity Log / order may reference them. `assign_staff` pinned them to the restaurant, which changes nothing; the pin goes with the disable. |
| 3 | **`damindyanawanjalee@gmail.com` carries the `Admin` Role Profile** (as does `Guest`, inertly). | A till user with every ERPNext manager role — HR, Accounts, Stock, System Manager. The app pins her to the restaurant, but Frappe Desk is wide open to her. A Role Profile also re-applies its roles on every save, so anything taken away by hand comes back. | Give her a floor profile (Cashier + Waiter, or a `Till` Role Profile with just those) and clear `Admin`. Kasun's profile was already cleared 2026-09-10 for the same reason. |
| 4 | **Nine `_Test Company*` rows** (and their `Cash - _TC*` accounts, Credit Card mode accounts) sit in the live Chart of Accounts. | Somebody ran the test suite against production once. They pollute company/account dropdowns and any consolidated report. | Delete the companies (Desk → Company → delete; each drags its accounts). Take a backup first; if a delete refuses because a document links to it, that document is the real problem — find it. |

### 7b. Operations

| # | What | Why it matters | Fix |
|---|---|---|---|
| 5 | **A POS shift has been open since 2026-09-01 09:21** (Dambulla). | Nine days of trade under one opening entry; the Z-report for it will be meaningless and the drawer difference huge. | Close it from `/pos` (Close shift). Then open shifts daily — the app ties open/close to login/logout, so make sure the tablet logs out at close. |
| 6 | **`PickMe` and `Credit Card` are booked to `Cash - LF`.** | There is no bank account on the `Luuv Fryxo` company at all, so `add_payment_mode` fell back to the cash account. Card and aggregator money is never in the drawer, so the cash ledger is overstated by every non-cash sale — the accountant will see it. | Create a Bank account under the company's Chart of Accounts, then Mode of Payment → PickMe / Credit Card → Accounts row → point at it. Existing invoices keep their old posting; only new ones move. |
| 7 | **Counter tablets still bookmark `/pos2`** (and the iMin app may open `/pos3`). Both are 301s to `/pos` for this release only. | The redirects go the release after next; a tablet still pointing at `/pos2` would then 404 mid-service. | Re-point every tablet's bookmark / home-screen icon to `/pos` during a close; check the iMin app's start URL. Then delete `www/pos2.*` and `www/pos3.*`. |
| 8 | **Two item images 404** on the public pages: `/files/cat_biryani.jpg`, `/files/cat_cheesekottu.jpg`, `/files/cat_beverage.jpg`. | Broken pictures on `/home` and in the POS grid for those items. Pre-existing, not from the deploy. | Upload the files again (Item → image) or clear the `image` field on those Items. |
| 9 | **`LuuvCRM Settings` is on defaults.** | The defaults *are* this restaurant's own values (name, Dambulla, phone, orange), so nothing looks wrong — but the moment anyone edits a page-level string expecting it to change, it will not; it lives in the Desk now. | Nothing to fix; tell the owner where the brand/phone/outlet line live (`/app/luuvcrm-settings`). `bench --site fryxo execute luuvcrm.settings.describe` prints them. |

### 7c. The host

| # | What | Why it matters | Fix |
|---|---|---|---|
| 10 | **`apps/` is not a Docker volume.** The backend's, the scheduler's and each queue worker's copy of every app lives in that container's writable layer. | `docker compose up --force-recreate`, an image pull or a rebuild **loses every deploy since the image was built** — for luuvcrm and for whatever the other tenants have had patched in the same way. `docker restart` is safe. | The real fix is in `frappe_docker`'s compose file (mount `apps` or bake them into the image) and belongs to whoever owns the VPS. Until then: keep `deploy-state/luuvcrm-<sha>.tar.gz` and the pre-deploy snapshots; re-running `deploy` (overlay only, migrate is a no-op) puts the code back. |
| 11 | **The first deploy shipped CRLF line endings** (git archive on Windows honoured `core.autocrlf`); the second deploy shipped LF, so the host now has a mix. | Harmless to Python, HTML and JS. It broke the script's md5 comparisons until they were made line-ending-blind. | Nothing on the host. The script now archives with `-c core.autocrlf=false -c core.eol=lf`. |
| 12 | **The moved-aside trees** `apps/luuvcrm.removed-20260910-092850/` and `…-101836/`, the 39 `.bak` files still in the package, and `/home/ubuntu/lf-deploy/*` (≈1.5 GB of tarballs, backups and logs). | Disk (103 GB free, so no rush) and confusion: a `.bak` next to a live file invites the wrong edit. | After a fortnight without a rollback: delete the `.removed-*` dirs and the `lf-deploy` run dirs older than the last deploy; move the `.bak` files out of the package. |
| 13 | **The three queue/scheduler containers run from a different image than the backend** and the two queue workers carry no `luuvcrm` at all. | Any luuvcrm job that ever lands on a queue worker (an `enqueue` from the app, a scheduled job routed to `queue-long`) fails with an import error. Today nothing enqueues, so nothing is broken. | If the app ever enqueues, the workers need the app installed — same compose problem as #10. |

### 7d. Before a second restaurant

| # | What | Why it matters | Fix |
|---|---|---|---|
| 14 | **Every `POS Invoice` has an empty `cost_center`**, and `Restaurant.cost_center` / `warehouse` are NULL. | Historical revenue belongs to no branch. The report helpers degrade to unscoped, so nothing breaks today — but branch-scoped invoice reports cannot exist until this is filled. | Create a Cost Center per branch (leaf, under the company), set `Restaurant.cost_center` + `warehouse`, set `POS Profile.cost_center` on each till, then backfill `POS Invoice.cost_center` from the profile that raised it (its own day, its own backup). |
| 15 | **`apply_strict_user_permissions` is off.** | With it off, a row with no `restaurant` stays visible to every branch. All history is backfilled now, and new rows stamp themselves, so the only unbranched rows left are an owner's writes made without choosing a branch (`branch.check` lists them). | Leave it off until `branch.check` has been empty for a while with two restaurants live; then decide. Turning it on with unbranched rows makes them vanish for every non-owner at once. |
| 16 | **Each branch needs its own `Restaurant Menu`, `Restaurant Table`s and till.** | hospitality's menu and tables belong to one Restaurant. A second Restaurant record with no menu serves an empty POS. | Copy the menu (or share prices through the Price List), create tables per branch, set `POS Profile.restaurant`, assign staff (`branch.assign`), run `branch.check` until it is empty. |
| 17 | **Staff who work two branches** (damindya and chandimal are on both the Dambulla and Kurunegala tills). | One User Permission pins a person to one branch; two pins show both. That is right for them, and wrong for a manager who should see only theirs. | Decide per person when the branches exist; `branch.assign(user, restaurant)` adds, `branch.unassign` removes. |

### 7e. Repo

| # | What | Fix |
|---|---|---|
| 18 | ~145 commits on `main` are not on `origin` (GitHub). | `git push origin main` from the owner's machine — the agent's push is blocked by policy. |
| 19 | ~~The docs describe the branch-picker as read-only.~~ **Done.** `docs/ROADMAP.md` §2c now lists `select()` as step 4 of `branch.current()` and says why the read-only principle was reversed for owners. `DEVELOPER_GUIDE.md` turned out not to carry the sentence at all. `docs/BRANCHES.md` was rewritten in the same pass — it still described `product` as live work and contradicted itself about the `host` remote. | — |

## 8. The week after

- **Fill in `LuuvCRM Settings`** if anything should differ from the defaults —
  though the defaults are already this restaurant's own name, town, phone and
  colour, so nothing looks different until somebody changes it.
  `bench --site <live> execute luuvcrm.settings.describe` prints every key,
  where it lives, and whether it is still on its default.
- **Watch `/errors` and `/activity`.** Both are working for the first time. The
  first week's rows are a backlog, not a regression.
- **Know that the app lives in container layers.** `apps/` is not a volume: the backend's copy, the scheduler's and each queue worker's are separate writable layers of separate containers. `docker restart` keeps them; `docker compose up --force-recreate`, an image pull or a rebuild **loses every deploy since the image was built**. Keep `deploy-state/luuvcrm-<sha>.tar.gz` and the pre-deploy snapshot; re-running `deploy` (overlay only, no migrate needed) puts the code back. Moving `apps/` onto a volume or into the image is the real fix and belongs to the compose file, not to this app.
- **The counter tablets** now land on `/pos` — `/pos2` and `/pos3` are 301s. Re-point the bookmarks during a close; the redirects go the release after.
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

## 9. The next deploy — what has landed on `main` since 2026-09-10

Everything below is committed and tested and **not yet on the live site**. Deploy
it with the same script and the same order as §3–§5: backup, rehearse, deploy,
verify. Nothing here changes a row the restaurant already has.

### 9a. It needs a migrate this time

| Patch | What it does | If it does not run |
|---|---|---|
| `add_purchaser_role` | Creates the `Purchaser` role and grants it read + create on `Purchase Bill` and `Expense`, read on `POS Approval` and `Expense Category`. Nobody holds it until the owner gives it to somebody in `/admin → Staff`. | Nobody can be given the role — it is not in the Staff screen's list until the Role row exists. The purchase endpoints are still closed to cashiers either way, because that is code, not the patch. |
| `add_table_enabled_field` | Adds `Restaurant Table.enabled`, default 1, so every existing table stays in service. | The code reads a missing column as "every table is enabled": the POS grid still shows every table, and `/admin → Tables` shows "migrate first" instead of the off switch. Nothing breaks; the new switch just is not there. |

The first deploy's `deploy` step already runs `bench migrate` — confirm in the
run log that `add_table_enabled_field` is listed as executed, not skipped.

### 9b. What changes, and who will notice

- **Signing in lands on `/staff`, not `/pos`.** Floor staff are forwarded straight
  to their own screen — a cashier to `/pos`, a waiter to `/waiter`, the kitchen to
  `/kitchen` — so the counter sees no difference. **The owner and managers, on a
  phone or in the installed app, see a set of tiles** (Admin, Manager, Purchases,
  POS, Waiter, Kitchen) and pick one; **at a desktop browser they go straight to
  `/admin`**. With more than one restaurant, the owner or admin **picks the
  restaurant first**, with last time's choice marked.
  This also fixes a waiter signing in from `/waiter`, who was being sent to the
  till and refused.
- **A "⇄ Apps" button, bottom-left, on POS, Waiter, Kitchen, Manager and
  Purchases** — only for somebody who can open more than one app. On `/admin` it
  is "⇄ Switch app" in the sidebar, where "Manager app ↗" used to be. **Check it
  on the counter tablet during a quiet moment:** if it sits on top of a POS
  button, `templates/includes/app_switch.html` is the one file to move it in.
- **One installable staff app.** Open `/staff` in Chrome on the tablet or phone →
  menu → *Install app* / *Add to Home screen*. It opens on the tiles, and moving
  between apps stays inside the app window. The per-app installs (`/manager`,
  `/purchases`) keep working; nobody has to reinstall.
- **Purchasing is a role of its own: `Purchaser`.** Give it in `/admin → Staff` to
  whoever buys for the restaurant. They can open `/purchases` and do everything on
  it — enter a bill, approve somebody else's, mark it paid — without being a
  manager. **Nobody can approve a bill they entered**, manager included, so one
  Purchaser alone cannot enter and approve their own purchase; it takes two people,
  or a Purchaser and a manager. **A cashier who is not a Purchaser can no longer
  reach purchasing** — no cashier screen ever offered it, but the endpoints used to
  accept them. Managers and the owner keep it. **Expenses are unchanged for the
  till**: a cashier can still record one; a Purchaser now can too, from the
  expenses tab on `/purchases`.
- **`/admin → Tables`.** Add a table (it is numbered after the branch's last one —
  there is no name to type), and turn one off. Off hides it from the POS grid, the
  waiter screen, the kiosk, the online order page and the QR sheet. **A bill
  already on a table that has been turned off still prices, prints and pays** —
  say that to whoever uses it first, because it is the thing they will worry about.
- **Nothing visible from the public-page branch work** while there is one
  restaurant. `/menu`, `/home`, `/order`, `/kiosk` and `/cus` only ask "which one
  are you at?" once a second restaurant exists and the link says nothing about
  which.

### 9c. Verify — add these to §5

```bash
# the staff manifest resolves and starts on the chooser
curl -s "https://<live>/api/method/luuvcrm.api.manifest?app=staff" | grep -E '"start_url"|"scope"'
#   "start_url": "/staff",   "scope": "/"

# the tables flag arrived
bench --site <live> execute frappe.db.has_column --args '["Restaurant Table", "enabled"]'
#   True
```

Then by hand, in this order:

1. Sign in as a **cashier** → you land on `/pos`, not on tiles.
2. Sign in as the **owner** on a phone → tiles. Sign in again at a desktop
   browser → straight to `/admin`; its sidebar "⇄ Switch app" still shows the tiles.
3. `/admin → Staff` → the role list includes **Purchaser**. Do not give it to a
   real person as a test; if you want to see it work, give it to a spare account,
   sign in as it, and confirm you land on `/purchases` and nowhere else.
4. `/admin → Tables` → every existing table is listed and **on**. Do not turn a
   real one off to test it; add a table, turn *that* off, confirm it left the POS
   grid, turn it back on.

### 9d. Tests that ran before this was committed

`test_launcher` (new, 16), `test_tables` (new, 13), `test_branch` (71, twice — see
the test-pollution fix in `bb2ffea`), `test_pages`, `test_menu`, `test_orders`,
`test_service`, `test_setup`, `test_api_package`, `test_dashboard`, `test_shifts`,
`test_brand`, `test_activity`, `test_approvals`, `test_purchases`, `test_roles`,
`test_security` — all passing.

**`test_security.test_wrong_pins_lock_the_number_out` depends on Redis.** It
failed once on the dev bench while Redis was down, and passed once Redis was back.
The PIN lockout counter lives in the Redis cache. `_pin_locked()` is written to
fail open when the cache is unreachable — a restaurant that loses Redis must not
also lose every customer login — so with no Redis the counter never counts. On
the live site Redis is up. If this test fails on a bench, check
`redis://127.0.0.1:13000` answers before reading anything into it.
