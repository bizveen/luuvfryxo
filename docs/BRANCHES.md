# Branches

One branch now: **`main`**.

## `main` — the restaurant, and the product

What Luuv Fryxo runs, and where all work happens. Features, fixes, structural work, anything the
client asks for. It went live on 2026-09-10.

Deploys with `deploy/vps-deploy.sh` (preflight → backup → rehearse → deploy) — see
`docs/DEPLOY_RUNBOOK.md` §4. **The live app directory is not a git checkout**, so the deploy is an
overlay of `git archive HEAD` over it, not a pull. That is why the script compares md5s and keeps
tarballs in `deploy-state/`: rollback is putting the last tarball back, not `git reset`.

## `product` — retired

`product` was the sellable version, split off so structural work — the schema into the repo, the
`api.py` split, the brand-from-settings rewrite — could happen without risking a live site
mid-service. All of it is merged: `git rev-list --left-right --count product...main` reads `0 62`.
The branch is kept as history and is never merged into or deployed. Nothing needs to come off it.

**Why the split ended.** The two-branch rule existed to protect a live site from a refactor. Once
the refactored version *became* the live site, the second branch was a place for work to sit
undeployed — which is the failure mode it was meant to prevent, wearing the other hat. `main` is
protected now by the deploy script's rehearsal and by the tests, not by a branch nobody ships.

If a change is ever genuinely too large to land on a live site in one go, branch for that change,
finish it, merge it, delete the branch. Do not keep a standing second branch.

## Remotes

| Remote | What it is | Push? |
|---|---|---|
| **`host`** | The owner's machine, over an SMB share (`192.168.1.8`). `git fetch host` needs the share mounted: `gio mount -a "smb://192.168.1.8/users"`. It is a non-bare checkout with `receive.denyCurrentBranch=updateInstead`, so a dirty file there rejects the push — it regularly carries whitespace-only diffs. Stash on the host, then push. | `main` only. Never `product`. |
| **`origin`** | GitHub (`bizveen/luuvfryxo`). Deliberately left behind — ~148 commits — and not used for deploys. | No. The owner pushes it if and when they want to. |

Neither remote is the live server. The VPS is reached by the deploy script over SSH; it has no git
repository on it at all.
