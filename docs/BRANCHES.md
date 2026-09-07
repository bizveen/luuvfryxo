# Branches

Two branches, two different jobs.

## `main` — the live restaurant

What Luuv Fryxo runs. Features, fixes, and anything the client asks for. Deploys to the `host` remote as it always has:

```bash
gio mount -a "smb://192.168.1.8/users"
git push host main
```

**Security fixes go here**, not on `product`. The three critical findings in `PRODUCTION_READINESS.md` §3 are open on the live site right now; fixing them on a branch nobody deploys helps nobody.

## `product` — the sellable version

Where LuuvCRM becomes software a second restaurant can install. Structural work only:

| Work | Why it is here and not on main |
|---|---|
| Schema into the repo (B1) | Touches every `setup_*` endpoint and adds ~26 DocType files. Large, and risky to land on a live site mid-service. |
| Delete the duplicate `POS Order` (B2) | Needs a migrate to verify; do it where a broken migrate costs nothing. |
| Tests + CI (B3) | Additive, but pairs with the schema work. |
| Declare dependencies (B4) | Only matters for a fresh install. |
| One brand from settings | Rewrites strings across all 25 pages. |
| Collapse `/pos`, `/pos2`, `/pos3` (done) | Deleted the file the restaurant is using. Landing it on main needs the counter bookmarks re-pointed in the same close — see the note under ROADMAP §2b. |
| Split `api.py` (done) | 7,338 lines moved at once — merges badly with anything, so it landed here in one commit. |
| Performance work (P1–P6) | Judgement call: see below. |

## Keeping them from diverging

This is the real risk. `product` will rewrite files that `main` is actively changing — `api.py` most of all.

**Rules that keep the merge survivable:**

1. **`main` → `product`, often.** Merge main into product at least weekly, and always before starting a big refactor. Never the other way round by accident.
2. **Small, mergeable things land on `main` first**, then flow down. Security fixes, bug fixes, a query limit, an index — all of these belong on `main` and reach `product` by merge.
3. **`product` only holds work that would be unsafe or pointless on the live site.** If a change is safe to deploy, it is a `main` change.
4. **Performance fixes: decide per fix.** An N+1 fix or a missing index is a `main` change — the restaurant feels the benefit immediately and it merges cleanly. Moving the activity logger off the request path, or introducing an asset build, is `product` work.
5. **Land the schema work as one merge, not a trickle.** It is the one change that genuinely cannot be split.

## Merging back

`product` is not merged into `main` as a whole. When a piece of it is finished and proven — the installer, say — it comes back as its own reviewed merge, after a fresh-site install test passes. The rest stays on `product` until it is equally ready.

If `product` ever becomes the version being sold, it becomes the new `main` and the live site upgrades onto it deliberately, with a backup and a rollback plan — not by drifting.

## Remotes

- **`host`** — the live server (SMB share). `main` deploys here.
- **`origin`** — GitHub. Not used for deploys.
