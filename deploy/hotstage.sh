#!/usr/bin/env bash
# hotstage.sh — put a handful of changed files on the live host now, and turn
# them on later. For a change too small to be worth the full vps-deploy.sh
# (no schema change, no migrate, no assets), but still on a till that is open.
#
#   deploy/hotstage.sh status  <file>...   what the live copies are, per container
#   deploy/hotstage.sh stage   <file>...   copy them in (backed up first, md5-verified, compiled)
#   deploy/hotstage.sh activate            restart backend + scheduler so the staged Python is read
#   deploy/hotstage.sh rollback            put the backed-up copies back (restart again if needed)
#
# Paths are relative to the repo root, exactly as git spells them, e.g.
#   deploy/hotstage.sh stage luuvcrm/roles.py luuvcrm/www/manager.html
#
# WHY STAGING IS SAFE WHILE THE SHOP IS OPEN
# Gunicorn runs with --preload: frappe and every app module were imported once,
# into the master, before the workers were forked. A .py file on disk is read at
# import and never again, so a staged .py changes nothing until a process
# restarts. That is the whole trick — the risky half of a deploy (copying) is
# the half with no downtime, and the cheap half (restarting) is the one that
# blips.
#
# WHAT DOES NOT HOLD FOR .html
# A www page is a Jinja template, re-read from disk. Staging one IS live. Only
# stage HTML that is harmless against the Python still running.
#
# WHY NOT JUST RELOAD GUNICORN
# It cannot be done on this host, and each dead end fails quietly rather than
# loudly, so they are written down:
#   SIGHUP  — Arbiter.reload() calls app.reload() (config only) and setup(),
#             and setup()'s preload branch calls app.wsgi(), which returns the
#             already-set self.callable. The app module is never re-imported.
#   SIGUSR2 — re-execs properly, but the master is PID 1 in the container, so
#             retiring the old master after the swap kills the PID namespace and
#             takes the container with it.
#   killing the workers — they are forked from the preloaded master, so the
#             replacements come up holding exactly the old code.
# A container restart is the only reload. Measured at ~4 s (three deploys in
# deploy-state), and it blips every tenant on frappe_docker-backend-1, not just
# fryxo.
set -euo pipefail

HOST=${HOST:-ubuntu@129.153.196.253}
KEY=${KEY:-$HOME/.ssh/fryxo.key}
BACKEND=frappe_docker-backend-1
# Every container that might carry a copy of the app. Which of them actually do
# is decided per container by the hooks.py probe, not by this list: as of
# 2026-09-12 the two queue workers have no luuvcrm at all.
CONTAINERS=${CONTAINERS:-"frappe_docker-backend-1 frappe_docker-scheduler-1 frappe_docker-queue-short-1 frappe_docker-queue-long-1"}
RESTARTABLE=${RESTARTABLE:-"frappe_docker-backend-1 frappe_docker-scheduler-1"}
SITE=fryxo
OTHER_SITE=clothing
BENCH=/home/frappe/frappe-bench
APP=$BENCH/apps/luuvcrm

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
STATE=${STATE:-$REPO/../../deploy-state}
mkdir -p "$STATE"; STATE=$(cd "$STATE" && pwd)
TS=$(date +%Y%m%d-%H%M%S)
RDIR=/home/ubuntu/lf-hotstage/$TS
MARK="$STATE/last-hotstage"

say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
die() { printf '\n\033[31mABORT: %s\033[0m\n' "$*" >&2; exit 1; }
SSH="ssh -i $KEY -o StrictHostKeyChecking=no -o ConnectTimeout=20 $HOST"
[ -f "$KEY" ] || die "ssh key not found at $KEY"

# md5 with CRLF stripped, the same normalisation vps-deploy.sh's preflight uses,
# so a file that only differs by line endings does not read as a difference.
md5_local() { tr -d '\r' < "$REPO/$1" | md5sum | cut -d' ' -f1; }

# ── status ───────────────────────────────────────────────────────────────────
status() {
  [ $# -gt 0 ] || die "usage: hotstage.sh status <file>..."
  say "live copies vs this working tree"
  local list; list=$(printf '%s\n' "$@")
  for f in "$@"; do printf '  repo  %s  %s\n' "$(md5_local "$f")" "$f"; done
  $SSH "CONTAINERS='$CONTAINERS' APP=$APP FILES='$list' bash -s" <<'EOF'
for c in $CONTAINERS; do
  if ! docker exec "$c" test -f "$APP/luuvcrm/hooks.py" 2>/dev/null; then
    echo "  $c: no luuvcrm here, skipped"; continue
  fi
  echo "  $c:"
  docker exec -e FILES="$FILES" -e APP="$APP" "$c" bash -c '
    cd "$APP" || exit 0
    for f in $FILES; do
      if [ -f "$f" ]; then printf "    live  %s  %s\n" "$(tr -d "\r" < "$f" | md5sum | cut -d" " -f1)" "$f"
      else printf "    live  %-32s %s\n" "MISSING" "$f"; fi
    done'
done
EOF
}

# ── stage ────────────────────────────────────────────────────────────────────
stage() {
  [ $# -gt 0 ] || die "usage: hotstage.sh stage <file>..."
  for f in "$@"; do [ -f "$REPO/$f" ] || die "no such file in the repo: $f"; done

  say "0. compile the Python here before anything is copied"
  local pys=()
  for f in "$@"; do case "$f" in *.py) pys+=("$REPO/$f");; esac; done
  if [ ${#pys[@]} -gt 0 ]; then
    python -m py_compile "${pys[@]}" || die "a staged .py does not compile"
    echo "   ${#pys[@]} file(s) compile"
  else
    echo "   no .py staged"
  fi

  say "1. ship the files to the host"
  $SSH "mkdir -p $RDIR"
  tar czf "$STATE/hotstage-$TS.tgz" -C "$REPO" "$@"
  scp -q -i "$KEY" -o StrictHostKeyChecking=no "$STATE/hotstage-$TS.tgz" "$HOST:$RDIR/files.tgz"
  printf '%s\n' "$@" > "$STATE/hotstage-$TS.list"
  scp -q -i "$KEY" -o StrictHostKeyChecking=no "$STATE/hotstage-$TS.list" "$HOST:$RDIR/files.list"
  echo "   $(printf '%s ' "$@")"

  say "2. back up, copy, verify, compile — per container"
  $SSH "TS=$TS RDIR=$RDIR APP=$APP BENCH=$BENCH CONTAINERS='$CONTAINERS' bash -s" <<'EOF'
set -e
BACKUP=$BENCH/apps/luuvcrm.hotstage-$TS
for c in $CONTAINERS; do
  if ! docker exec "$c" test -f "$APP/luuvcrm/hooks.py" 2>/dev/null; then
    echo "  $c: no luuvcrm here, skipped"; continue
  fi
  echo "  $c:"
  docker cp "$RDIR/files.tgz"  "$c:/tmp/lf-hotstage.tgz"
  docker cp "$RDIR/files.list" "$c:/tmp/lf-hotstage.list"
  docker exec -i -u root -e APP="$APP" -e BACKUP="$BACKUP" "$c" bash -s <<'IEOF'
set -e
rm -rf /tmp/lf-hs && mkdir -p /tmp/lf-hs
tar xzf /tmp/lf-hotstage.tgz -C /tmp/lf-hs
mkdir -p "$BACKUP"
while read -r f; do
  [ -n "$f" ] || continue
  # The rollback source. A file that is not there yet is recorded as such, so
  # rollback removes it rather than restoring a file that never existed.
  if [ -f "$APP/$f" ]; then
    mkdir -p "$BACKUP/$(dirname "$f")"
    cp -a "$APP/$f" "$BACKUP/$f"
  else
    mkdir -p "$BACKUP/$(dirname "$f")"
    : > "$BACKUP/$f.WAS-ABSENT"
  fi
  mkdir -p "$APP/$(dirname "$f")"
  cp -a "/tmp/lf-hs/$f" "$APP/$f"
  chown 1001:1001 "$APP/$f"
  new=$(tr -d '\r' < "/tmp/lf-hs/$f" | md5sum | cut -d' ' -f1)
  got=$(tr -d '\r' < "$APP/$f"       | md5sum | cut -d' ' -f1)
  [ "$new" = "$got" ] || { echo "    MD5 MISMATCH after copy: $f"; exit 1; }
  printf '    ok  %s  %s\n' "$got" "$f"
  # A .pyc compiled from the old source would be used only if its mtime/size
  # header still matched, which it will not — but a stale one is noise in a
  # traceback, so it goes.
  case "$f" in *.py)
    d=$(dirname "$APP/$f"); b=$(basename "$f" .py)
    rm -f "$d/__pycache__/$b."*.pyc 2>/dev/null || true ;;
  esac
done < /tmp/lf-hotstage.list
chown -R 1001:1001 "$BACKUP"
PYTHONPYCACHEPREFIX=/tmp/lf-pyc python3 -m compileall -q "$APP/luuvcrm" >/tmp/lf-hs-compile.log 2>&1 \
  || { cat /tmp/lf-hs-compile.log; echo "    COMPILE FAILED"; exit 1; }
echo "    whole package compiles"
rm -rf /tmp/lf-hs /tmp/lf-hotstage.tgz /tmp/lf-hotstage.list
IEOF
done
echo "  backup dir on each container: $BACKUP"
EOF

  printf 'TS=%s\nRDIR=%s\nBACKUP=%s\nFILES=%s\n' \
    "$TS" "$RDIR" "$BENCH/apps/luuvcrm.hotstage-$TS" "$*" > "$MARK"
  say "staged — nothing is live yet for .py (gunicorn --preload holds the old import)"
  echo "  turn it on with:  deploy/hotstage.sh activate     (~4 s blip, every tenant)"
  echo "  undo the files:   deploy/hotstage.sh rollback"
}

# ── activate ─────────────────────────────────────────────────────────────────
activate() {
  [ -f "$MARK" ] || die "nothing staged (no $MARK)"
  say "restarting $RESTARTABLE — the only way a --preload gunicorn re-imports"
  $SSH "BACKEND=$BACKEND RESTARTABLE='$RESTARTABLE' BENCH=$BENCH SITE=$SITE OTHER_SITE=$OTHER_SITE bash -s" <<'EOF'
set -e
for c in $RESTARTABLE; do
  t0=$(date +%s)
  docker restart "$c" >/dev/null
  if [ "$c" = "$BACKEND" ]; then
    code=""
    for i in $(seq 1 60); do
      code=$(docker exec "$BACKEND" curl -s -o /dev/null -w '%{http_code}' \
               -H "Host: $OTHER_SITE" http://localhost:8000/api/method/ping || true)
      [ "$code" = 200 ] && break
      sleep 1
    done
    [ "$code" = 200 ] || { echo "  $c did NOT come back (ping=$code)"; exit 1; }
    echo "  $c back in $(( $(date +%s) - t0 )) s"
  else
    echo "  $c restarted"
  fi
done
docker exec -w "$BENCH" "$BACKEND" bench --site "$SITE" clear-cache
docker exec -w "$BENCH" "$BACKEND" bench --site "$SITE" clear-website-cache
echo "  caches cleared on $SITE"
EOF
  say "activated"
}

# ── rollback ─────────────────────────────────────────────────────────────────
rollback() {
  [ -f "$MARK" ] || die "nothing staged (no $MARK)"
  local bts; bts=$(sed -n 's/^TS=//p' "$MARK")
  say "restoring the pre-stage copies from luuvcrm.hotstage-$bts"
  $SSH "TS=$bts APP=$APP BENCH=$BENCH CONTAINERS='$CONTAINERS' bash -s" <<'EOF'
set -e
BACKUP=$BENCH/apps/luuvcrm.hotstage-$TS
for c in $CONTAINERS; do
  docker exec "$c" test -d "$BACKUP" 2>/dev/null || { echo "  $c: no backup there, skipped"; continue; }
  echo "  $c:"
  docker exec -i -u root -e APP="$APP" -e BACKUP="$BACKUP" "$c" bash -s <<'IEOF'
set -e
cd "$BACKUP"
find . -type f | sed 's|^\./||' | while read -r f; do
  case "$f" in
    *.WAS-ABSENT) t=${f%.WAS-ABSENT}; rm -f "$APP/$t"; echo "    removed (was absent)  $t" ;;
    *)            cp -a "$BACKUP/$f" "$APP/$f"; chown 1001:1001 "$APP/$f"; echo "    restored  $f" ;;
  esac
done
find "$APP/luuvcrm" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
IEOF
done
EOF
  say "files restored — if you had already activated, run: deploy/hotstage.sh activate"
  echo "  (activate just restarts; it is what makes the restored .py the live one again)"
}

cmd=${1:-}; shift || true
case "$cmd" in
  status)   status "$@" ;;
  stage)    stage "$@" ;;
  activate) activate ;;
  rollback) rollback ;;
  *) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 1 ;;
esac
