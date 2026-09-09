#!/usr/bin/env bash
# vps-deploy.sh — deploy this repo to the live host, in the phases the runbook
# names. Run from Git Bash on the PC; every remote step goes over ssh + docker.
#
#   deploy/vps-deploy.sh preflight   read-only: what would change, what is kept, what is removed
#   deploy/vps-deploy.sh backup      fryxo DB + files backup, and a tar of the live app dir, both copied to this PC
#   deploy/vps-deploy.sh rehearse    restore that backup into a throwaway site + container, migrate it there
#   deploy/vps-deploy.sh deploy      the real thing: maintenance on → overlay → restart → migrate → assets → smoke
#   deploy/vps-deploy.sh smoke       the checks alone (public URLs, the clothing site, drift, the menu)
#   deploy/vps-deploy.sh rollback    put the pre-deploy app dir back; add --db to restore the database too
#
# Why this exists (docs/DEPLOY_RUNBOOK.md §4): the live app directory is not a
# git checkout, not a volume, and not only ours — it holds the clothing site's
# code (live-only/), .bak files and scratch scripts. So the deploy is an
# OVERLAY: the repo's files are copied over the live tree, only the paths in
# deploy/REMOVE.list are moved aside, and everything else is left exactly where
# it was. The scheduler and queue containers keep their own copies of the app
# and get the same overlay. Nothing here runs `git` on the host.
#
# Env you can set: HOST, KEY, DB_ROOT_PASSWORD (rehearse, rollback --db),
# RUN_TESTS=1 (rehearse: run the suite on the throwaway site), FORCE=1 (deploy
# without a fresh backup / passed rehearsal — do not).
set -euo pipefail

HOST=${HOST:-ubuntu@129.153.196.253}
KEY=${KEY:-$HOME/.ssh/fryxo.key}          # a chmod-600 copy of Desktop/oracleKey/ssh-key-2026-02-25.key
BACKEND=frappe_docker-backend-1
WORKERS=${WORKERS:-"frappe_docker-scheduler-1 frappe_docker-queue-short-1 frappe_docker-queue-long-1"}
SITE=fryxo
OTHER_SITE=clothing                        # the site that carries the app without being a restaurant
PUBLIC=https://luuvgrand.com
BENCH=/home/frappe/frappe-bench
APP=$BENCH/apps/luuvcrm
NET=frappe_docker_frappe_network
REHEARSE_SITE=rehearse.localhost

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
STATE=${STATE:-$REPO/../../deploy-state}   # outside the repo: tarballs, backups, logs, markers
mkdir -p "$STATE"
STATE=$(cd "$STATE" && pwd)
TS=$(date +%Y%m%d-%H%M%S)
SHA=$(git -C "$REPO" rev-parse --short HEAD)
RDIR=/home/ubuntu/lf-deploy/$TS            # per-run scratch on the host

# ─── plumbing ────────────────────────────────────────────────────────────────
say()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
die()  { printf '\n\033[31mABORT: %s\033[0m\n' "$*" >&2; exit 1; }
SSH="ssh -i $KEY -o StrictHostKeyChecking=no -o ConnectTimeout=20 $HOST"
# Run a bash script on the host with the run's parameters in its environment.
# Use a QUOTED heredoc (<<'EOF') so nothing expands locally.
remote() {
  $SSH "TS=$TS SHA=$SHA BENCH=$BENCH APP=$APP BACKEND=$BACKEND SITE=$SITE OTHER_SITE=$OTHER_SITE \
        NET=$NET RDIR=$RDIR REHEARSE_SITE=$REHEARSE_SITE WORKERS='$WORKERS' \
        DB_ROOT_PASSWORD='${DB_ROOT_PASSWORD:-}' RUN_TESTS='${RUN_TESTS:-}' bash -s"
}
upload() { scp -q -i "$KEY" -o StrictHostKeyChecking=no "$1" "$HOST:$2"; }
download() { scp -q -i "$KEY" -o StrictHostKeyChecking=no "$HOST:$1" "$2"; }
http() { curl -s -o /dev/null -w '%{http_code}' --max-time 25 "$@"; }

[ -f "$KEY" ] || die "ssh key not found at $KEY — copy the Oracle key there and chmod 600 it"

# The overlay, as a shell function the host runs against one container. It is
# the same for the backend, the workers and the rehearsal container.
OVERLAY_FN='
overlay_into() {  # $1 = container
  local c=$1
  docker exec "$c" test -f "$APP/luuvcrm/hooks.py" || { echo "  $c: no luuvcrm here, skipped"; return 0; }
  docker cp "$RDIR/luuvcrm-$SHA.tar.gz" "$c:/tmp/luuvcrm.tgz"
  docker cp "$RDIR/REMOVE.list" "$c:/tmp/REMOVE.list"
  docker exec -u root -e APP="$APP" -e BENCH="$BENCH" -e TS="$TS" "$c" bash -s <<'"'"'IEOF'"'"'
set -e
rm -rf /tmp/lf-overlay && mkdir -p /tmp/lf-overlay
tar xzf /tmp/luuvcrm.tgz -C /tmp/lf-overlay
cp -a /tmp/lf-overlay/luuvcrm/. "$APP/"
REMOVED=$BENCH/apps/luuvcrm.removed-$TS
grep -v "^[[:space:]]*#" /tmp/REMOVE.list | sed "s/[[:space:]]*$//" | grep -v "^$" | while read -r p; do
  if [ -e "$APP/$p" ]; then
    mkdir -p "$REMOVED/$(dirname "$p")"
    mv "$APP/$p" "$REMOVED/$p"
    echo "  moved aside: $p"
  fi
done
# The clothing site'"'"'s code, which the repo keeps outside the package.
cp -a "$APP/live-only/zeloura/luuvcrm/." "$APP/luuvcrm/"
find "$APP" -name __pycache__ -type d -prune -exec rm -rf {} +
chown -R 1001:1001 "$APP"
rm -rf /tmp/lf-overlay /tmp/luuvcrm.tgz /tmp/REMOVE.list
# Every module compiles — before anything restarts on it. Bytecode goes to /tmp.
PYTHONPYCACHEPREFIX=/tmp/lf-pyc python3 -m compileall -q "$APP/luuvcrm" >/tmp/lf-compile.log 2>&1 \
  || { cat /tmp/lf-compile.log; echo "COMPILE FAILED in $APP"; exit 1; }
rm -rf /tmp/lf-pyc
echo "  overlay ok: $(find "$APP/luuvcrm" -name "*.py" | wc -l) modules compile"
IEOF
}
'

# ─── preflight ───────────────────────────────────────────────────────────────
preflight() {
  say "preflight $SHA (read-only)"
  cd "$REPO"
  [ -z "$(git status --porcelain)" ] || die "working tree is dirty — commit first; the tarball is built from HEAD"
  [ "$(git rev-parse --abbrev-ref HEAD)" = "main" ] || echo "note: not on main ($(git rev-parse --abbrev-ref HEAD))"

  echo "-- every module compiles (locally)"
  python -m compileall -q luuvcrm live-only >/dev/null || die "compile errors"

  echo "-- Jinja traps in www (.__ and {# both 417 through Frappe's renderer)"
  if grep -l '\.__' luuvcrm/www/*.js luuvcrm/www/*.html luuvcrm/templates/includes/*.html 2>/dev/null; then die "a '.__' sequence in a served file"; fi
  if grep -l '{#' luuvcrm/www/*.js luuvcrm/www/*.html luuvcrm/templates/includes/*.html 2>/dev/null; then die "a '{#' sequence in a served file"; fi
  echo "-- tag balance in every page (an unclosed <style> once blanked /manager)"
  for f in luuvcrm/www/*.html; do
    for t in style script; do
      o=$(grep -o "<$t[ >]" "$f" | wc -l); c=$(grep -o "</$t>" "$f" | wc -l)
      [ "$o" = "$c" ] || die "$f: <$t> opened $o times, closed $c"
    done
  done

  echo "-- tarball"
  git archive --format=tar.gz --prefix=luuvcrm/ -o "$STATE/luuvcrm-$SHA.tar.gz" HEAD
  ls -l "$STATE/luuvcrm-$SHA.tar.gz"

  echo "-- live manifest"
  $SSH "docker exec $BACKEND bash -c 'cd $APP && find . -type f -not -path \"./.git/*\" -not -path \"*/__pycache__/*\" -not -name \"*.pyc\" | sort | xargs md5sum'" \
    | sed 's#  \./#  #' > "$STATE/live-manifest-$TS.txt"
  echo "   $(wc -l < "$STATE/live-manifest-$TS.txt") files on the host"

  echo "-- what the overlay does to each of them"
  local report="$STATE/preflight-$SHA.txt"
  : > "$report"
  local same=0 changed=0 new=0
  while read -r path; do
    local live blob
    live=$(grep " ${path}$" "$STATE/live-manifest-$TS.txt" | cut -d' ' -f1 || true)
    blob=$(git cat-file -p "HEAD:$path" | md5sum | cut -d' ' -f1)
    if [ -z "$live" ]; then echo "NEW      $path" >> "$report"; new=$((new+1))
    elif [ "$live" = "$blob" ]; then same=$((same+1))
    else echo "CHANGED  $path" >> "$report"; changed=$((changed+1)); fi
  done < <(git ls-files)
  in_remove_list() {
    local p=$1 r
    while read -r r; do case "$p" in "$r"|"$r"/*) return 0;; esac; done < <(grep -v '^\s*#' deploy/REMOVE.list | sed 's/\s*$//' | grep -v '^$')
    return 1
  }
  local removed=0 kept=0
  while read -r live_path; do
    if git ls-files --error-unmatch "$live_path" >/dev/null 2>&1; then continue; fi
    if in_remove_list "$live_path"; then
      echo "REMOVE   $live_path" >> "$report"; removed=$((removed+1))
    else
      echo "KEPT     $live_path" >> "$report"; kept=$((kept+1))
    fi
  done < <(awk '{print $2}' "$STATE/live-manifest-$TS.txt")
  echo "   unchanged $same · changed $changed · new $new · moved aside $removed · left alone $kept"
  echo "   full list: $report"

  echo "-- the clothing site's code: repo copy vs host copy"
  local drift=0
  while read -r f; do
    local rel=${f#live-only/zeloura/}
    local live; live=$(grep " ${rel}$" "$STATE/live-manifest-$TS.txt" | cut -d' ' -f1 || true)
    local mine; mine=$(md5sum "$f" | cut -d' ' -f1)
    local mine_lf; mine_lf=$(tr -d '\r' < "$f" | md5sum | cut -d' ' -f1)
    if [ -n "$live" ] && [ "$live" != "$mine" ] && [ "$live" != "$mine_lf" ]; then echo "   DIFFERS: $rel (host is newer — pull it into live-only/ first)"; drift=1; fi
  done < <(git ls-files live-only | grep -v README)
  [ "$drift" = 0 ] && echo "   identical" || die "live-only/ is behind the host"

  echo "-- $OTHER_SITE: luuvcrm_passive"
  $SSH "docker exec $BACKEND python3 -c \"import json;print('   set' if json.load(open('$BENCH/sites/$OTHER_SITE/site_config.json')).get('luuvcrm_passive') else '   NOT SET yet — deploy sets it (one key in that site_config.json)')\""

  grep -E '^(CHANGED|NEW|REMOVE)' "$report" | sed 's/^/   /'
  echo "preflight ok — tarball $STATE/luuvcrm-$SHA.tar.gz"
}

# ─── backup ──────────────────────────────────────────────────────────────────
backup() {
  say "backup $SITE + live app dir → $STATE/backups/$TS"
  mkdir -p "$STATE/backups/$TS"
  remote <<'EOF'
set -e
mkdir -p "$RDIR"
docker exec "$BACKEND" bash -c "cd $BENCH && bench --site $SITE backup --with-files" | tee "$RDIR/backup.out"
newest=$(docker exec "$BACKEND" bash -c "ls -t $BENCH/sites/$SITE/private/backups/*-database.sql.gz | head -1")
stamp=$(basename "$newest" | cut -d- -f1)
echo "backup stamp $stamp"
for f in $(docker exec "$BACKEND" bash -c "ls $BENCH/sites/$SITE/private/backups/ | grep ^$stamp"); do
  docker cp "$BACKEND:$BENCH/sites/$SITE/private/backups/$f" "$RDIR/$f"
done
size=$(stat -c %s "$RDIR/$stamp"*-database.sql.gz)
[ "$size" -gt 300000 ] || { echo "database dump is only $size bytes — a failed backup that exited 0"; exit 1; }
grep -q encryption_key "$RDIR/$stamp"*-site_config_backup.json || { echo "no encryption_key in the site_config backup"; exit 1; }
echo "$stamp" > "$RDIR/backup-stamp"
echo "-- live app dir snapshot (everything except .git and bytecode)"
docker exec -u root "$BACKEND" bash -c "cd $BENCH/apps && tar czf /tmp/luuvcrm-live.tgz --exclude=.git --exclude=__pycache__ luuvcrm"
docker cp "$BACKEND:/tmp/luuvcrm-live.tgz" "$RDIR/luuvcrm-live-$TS.tgz"
docker exec -u root "$BACKEND" rm -f /tmp/luuvcrm-live.tgz
ls -lh "$RDIR"
EOF
  for f in $($SSH "ls $RDIR"); do download "$RDIR/$f" "$STATE/backups/$TS/$f"; done
  ls -lh "$STATE/backups/$TS"
  printf 'TS=%s\nSTAMP=%s\nRDIR=%s\n' "$TS" "$(cat "$STATE/backups/$TS/backup-stamp")" "$RDIR" > "$STATE/last-backup"
  echo "backup ok — off the box at $STATE/backups/$TS (keep it; rollback --db uses the stamp in $STATE/last-backup)"
}

# ─── rehearse ────────────────────────────────────────────────────────────────
rehearse() {
  say "rehearse $SHA on $REHEARSE_SITE in a throwaway container"
  [ -n "${DB_ROOT_PASSWORD:-}" ] || die "DB_ROOT_PASSWORD is needed for new-site/restore/drop-site"
  [ -f "$STATE/last-backup" ] || die "run backup first"
  [ -f "$STATE/luuvcrm-$SHA.tar.gz" ] || die "run preflight first (builds the tarball)"
  local STAMP; STAMP=$(sed -n 's/^STAMP=//p' "$STATE/last-backup")
  [ -n "$STAMP" ] || die "no backup stamp in $STATE/last-backup"
  $SSH "mkdir -p $RDIR"
  upload "$STATE/luuvcrm-$SHA.tar.gz" "$RDIR/"
  upload "$REPO/deploy/REMOVE.list" "$RDIR/"
  local rlog="$STATE/rehearse-$SHA-$TS.log"
  remote <<EOF | tee "$rlog"
$OVERLAY_FN
set -e
STAMP=$STAMP
image=\$(docker inspect "\$BACKEND" --format '{{.Config.Image}}')
docker rm -f lf-rehearse >/dev/null 2>&1 || true
docker run -d --name lf-rehearse --network "\$NET" \\
  -v frappe_docker_sites:\$BENCH/sites -v frappe_docker_assets:\$BENCH/sites/assets -v frappe_docker_logs:\$BENCH/logs \\
  -w "\$BENCH" "\$image" sleep infinity >/dev/null
echo "-- overlay into the throwaway container"
overlay_into lf-rehearse
B="docker exec -w \$BENCH lf-rehearse"
echo "-- new site + restore \$STAMP"
\$B bench drop-site "\$REHEARSE_SITE" --db-root-password "\$DB_ROOT_PASSWORD" --force --no-backup >/dev/null 2>&1 || true
\$B bench new-site "\$REHEARSE_SITE" --db-root-password "\$DB_ROOT_PASSWORD" --admin-password admin --mariadb-user-host-login-scope='%' \\
  || \$B bench new-site "\$REHEARSE_SITE" --db-root-password "\$DB_ROOT_PASSWORD" --admin-password admin --no-mariadb-socket
\$B bench --site "\$REHEARSE_SITE" restore "\$BENCH/sites/\$SITE/private/backups/\$STAMP-\$SITE-database.sql.gz" --db-root-password "\$DB_ROOT_PASSWORD"
echo "-- migrate"
set +e
\$B bash -c "bench --site \$REHEARSE_SITE migrate" > "\$RDIR/rehearse-migrate.log" 2>&1
rc=\$?
set -e
echo "migrate exit=\$rc"
grep -nEi "^Error|Traceback|orphan|deleted|does not exist" "\$RDIR/rehearse-migrate.log" && { echo "MIGRATE LOG HAS ERRORS"; exit 1; } || echo "migrate log clean"
[ "\$rc" = 0 ] || exit 1
echo "-- drift / branch / settings / menu"
\$B bench --site "\$REHEARSE_SITE" execute luuvcrm.setup_doctype.report_drift | tee "\$RDIR/drift.txt"
! grep -Eq "^missing .*: [1-9]" "\$RDIR/drift.txt" || { echo "DRIFT AFTER MIGRATE"; exit 1; }
\$B bench --site "\$REHEARSE_SITE" execute luuvcrm.branch.check
\$B bench --site "\$REHEARSE_SITE" execute luuvcrm.settings.describe
echo "   menu items: \$(\$B bench --site "\$REHEARSE_SITE" execute luuvcrm.api.get_menu_data | grep -o '"item_name"' | wc -l)"
echo "-- render the public pages through a dev server inside the container"
docker exec -d -w "\$BENCH" lf-rehearse bash -c "bench serve --port 8001 > /tmp/serve.log 2>&1"
sleep 12
for p in / /home /order /menu /pos-login /api/method/ping /api/method/luuvcrm.zeloura_api.get_csrf; do
  code=\$(docker exec lf-rehearse curl -s -o /dev/null -w '%{http_code}' -H "Host: \$REHEARSE_SITE" "http://127.0.0.1:8001\$p")
  echo "   \$p -> \$code"
  case "\$p" in /api/method/luuvcrm.zeloura_api.get_csrf) ;; *) [ "\$code" = 200 ] || { echo "PAGE FAILED: \$p"; docker exec lf-rehearse tail -30 /tmp/serve.log; exit 1; };; esac
done
body=\$(docker exec lf-rehearse curl -s -H "Host: \$REHEARSE_SITE" http://127.0.0.1:8001/home)
echo "\$body" | grep -q "menu" || { echo "/home rendered without the menu"; exit 1; }
if [ -n "\$RUN_TESTS" ]; then
  echo "-- test suite (allowed here: not a production site)"
  \$B bench --site "\$REHEARSE_SITE" set-config allow_tests true
  \$B bash -c "bench --site \$REHEARSE_SITE run-tests --app luuvcrm" > "\$RDIR/rehearse-tests.log" 2>&1 || { tail -40 "\$RDIR/rehearse-tests.log"; echo "TESTS FAILED"; exit 1; }
  tail -5 "\$RDIR/rehearse-tests.log"
fi
echo "-- clean up"
\$B bench drop-site "\$REHEARSE_SITE" --db-root-password "\$DB_ROOT_PASSWORD" --force --no-backup
docker rm -f lf-rehearse >/dev/null
echo "REHEARSAL PASS \$SHA"
EOF
  download "$RDIR/rehearse-migrate.log" "$STATE/rehearse-migrate-$SHA-$TS.log" || true
  grep -q "REHEARSAL PASS $SHA" "$rlog" || die "rehearsal did not pass — see $rlog"
  printf 'SHA=%s\nTS=%s\n' "$SHA" "$TS" > "$STATE/last-rehearsal"
  echo "rehearsal ok — $rlog"
}

# ─── deploy ──────────────────────────────────────────────────────────────────
deploy() {
  say "deploy $SHA → $SITE"
  [ -f "$STATE/luuvcrm-$SHA.tar.gz" ] || die "run preflight first"
  if [ -z "${FORCE:-}" ]; then
    [ -f "$STATE/last-backup" ] || die "no backup — run backup"
    local bts; bts=$(sed -n 's/^TS=//p' "$STATE/last-backup")
    local when; when=$(date -d "${bts%%-*} $(echo "${bts##*-}" | sed 's/\(..\)\(..\)\(..\)/\1:\2:\3/')" +%s 2>/dev/null || echo 0)
    local age=$(( $(date +%s) - when ))
    [ "$age" -lt 43200 ] || die "the last backup ($bts) is older than 12 hours — take a fresh one"
    grep -q "SHA=$SHA" "$STATE/last-rehearsal" 2>/dev/null || die "no passed rehearsal for $SHA — run rehearse"
  fi
  # the deploy's own TS is the run's; the backup's TS came from last-backup
  local BTS; BTS=$(sed -n 's/^TS=//p' "$STATE/last-backup" 2>/dev/null || echo "none")
  echo "covered by backup $BTS ($STATE/backups/$BTS) — say so out loud before continuing"
  $SSH "mkdir -p $RDIR"
  upload "$STATE/luuvcrm-$SHA.tar.gz" "$RDIR/"
  upload "$REPO/deploy/REMOVE.list" "$RDIR/"
  local dlog="$STATE/deploy-$SHA-$TS.log"
  remote <<EOF | tee "$dlog"
$OVERLAY_FN
set -e
B="docker exec -w \$BENCH \$BACKEND"
echo "-- 0. \$OTHER_SITE stays a clothing shop: luuvcrm_passive"
\$B bench --site "\$OTHER_SITE" set-config luuvcrm_passive 1
docker exec "\$BACKEND" python3 -c "import json;assert json.load(open('\$BENCH/sites/\$OTHER_SITE/site_config.json'))['luuvcrm_passive']==1;print('   set')"
echo "-- 1. maintenance mode on (\$SITE only)"
\$B bench --site "\$SITE" set-maintenance-mode on
echo "-- 2. pre-deploy snapshot of the live app dir (the rollback source)"
docker exec -u root "\$BACKEND" bash -c "cd \$BENCH/apps && tar czf /tmp/pre.tgz --exclude=__pycache__ luuvcrm"
docker cp "\$BACKEND:/tmp/pre.tgz" "\$RDIR/luuvcrm-pre-deploy.tgz"
docker exec -u root "\$BACKEND" rm -f /tmp/pre.tgz
ls -lh "\$RDIR/luuvcrm-pre-deploy.tgz"
echo "-- 3. overlay: backend"
overlay_into "\$BACKEND"
echo "-- 4. overlay: workers (each keeps its own copy of the app)"
for w in \$WORKERS; do echo "   \$w"; overlay_into "\$w"; done
echo "-- 5. restart the backend (gunicorn --preload; every tenant blips ~15 s)"
docker restart "\$BACKEND" >/dev/null
for i in \$(seq 1 40); do
  code=\$(curl -s -o /dev/null -w '%{http_code}' -H "Host: \$OTHER_SITE" http://localhost:8084/api/method/ping || true)
  [ "\$code" = 200 ] && break; sleep 2
done
[ "\$code" = 200 ] || { echo "backend did not come back (ping via \$OTHER_SITE = \$code)"; exit 1; }
echo "   back after ~\$((i*2)) s"
echo "-- 6. migrate \$SITE"
set +e
\$B bash -c "bench --site \$SITE migrate" > "\$RDIR/migrate.log" 2>&1
rc=\$?
set -e
echo "migrate exit=\$rc"
grep -nEi "^Error|Traceback|orphan|deleted|does not exist" "\$RDIR/migrate.log" && echo "MIGRATE LOG HAS ERRORS" || echo "migrate log clean"
if [ "\$rc" != 0 ]; then echo "MIGRATE FAILED — site is still in maintenance mode. Run: deploy/vps-deploy.sh rollback --db"; exit 1; fi
echo "-- 7. assets: the frontends serve sites/assets from their own image copy through the symlink; make it a real copy"
docker exec -u root -e APP="\$APP" -e BENCH="\$BENCH" "\$BACKEND" bash -c '
  A=\$BENCH/sites/assets/luuvcrm
  if [ -L "\$A" ]; then rm "\$A"; cp -r "\$APP/luuvcrm/public" "\$A"; echo "   symlink replaced by a copy"; else cp -r "\$APP/luuvcrm/public/." "\$A/"; echo "   copy refreshed"; fi
  chown -R 1000:1000 "\$A"'
echo "-- 8. caches, maintenance off"
\$B bench --site "\$SITE" clear-cache
\$B bench --site "\$SITE" clear-website-cache
\$B bench --site "\$SITE" set-maintenance-mode off
echo "-- 9. restart the workers"
for w in \$WORKERS; do docker restart "\$w" >/dev/null && echo "   \$w"; done
echo "DEPLOY DONE \$SHA"
EOF
  download "$RDIR/migrate.log" "$STATE/migrate-$SHA-$TS.log" || true
  grep -q "DEPLOY DONE $SHA" "$dlog" || die "deploy did not finish — read $dlog; the pre-deploy snapshot is $RDIR/luuvcrm-pre-deploy.tgz on the host"
  printf 'SHA=%s\nTS=%s\nRDIR=%s\nSNAPSHOT=%s/luuvcrm-pre-deploy.tgz\n' "$SHA" "$TS" "$RDIR" "$RDIR" > "$STATE/last-deploy"
  smoke
  echo "deploy ok — log $dlog. Commit nothing: HEAD $SHA is what is live."
}

# ─── smoke ───────────────────────────────────────────────────────────────────
smoke() {
  say "smoke"
  local fail=0
  echo "-- $PUBLIC"
  for p in / /home /order /menu /pos-login /kiosk /track /cus /api/method/ping /assets/luuvcrm/app/icon-192.png; do
    local code; code=$(http "$PUBLIC$p")
    printf '   %-40s %s\n' "$p" "$code"
    [ "$code" = 200 ] || fail=1
  done
  for p in /pos /pos2 /pos3 /manager /purchases /admin /errors /activity; do
    local code; code=$(http "$PUBLIC$p")           # staff pages: a redirect to login is fine, a 5xx is not
    printf '   %-40s %s\n' "$p" "$code"
    case "$code" in 2*|3*|403) ;; *) fail=1;; esac
  done
  local body; body=$(curl -s --max-time 25 "$PUBLIC/home")
  echo "$body" | grep -qi "menu" || { echo "   /home has no menu in it"; fail=1; }
  echo "$body" | grep -q "Server Error" && { echo "   /home is an error page"; fail=1; }
  echo "-- $OTHER_SITE (through the host, by Host header)"
  remote <<'EOF' || fail=1
set -e
for p in / /home /login /api/method/luuvcrm.zeloura_api.get_csrf; do
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: $OTHER_SITE" "http://localhost:8084$p")
  printf '   %-40s %s\n' "$p" "$code"
  [ "$code" = 200 ] || exit 1
done
home=$(curl -s -H "Host: $OTHER_SITE" http://localhost:8084/)
echo "$home" | grep -q "Luuv Fryxo" && { echo "   the clothing site's front door says Luuv Fryxo"; exit 1; }
echo "$home" | grep -qi "zeloura" || { echo "   the clothing site's front door is not Zeloura's"; exit 1; }
login=$(curl -s -H "Host: $OTHER_SITE" http://localhost:8084/login)
echo "$login" | grep -q "Luuv Fryxo" && { echo "   the clothing site's login page says Luuv Fryxo"; exit 1; }
echo "   its own pages, its own name"
echo "-- $SITE: drift, branch, menu"
B="docker exec -w $BENCH $BACKEND"
$B bench --site "$SITE" execute luuvcrm.setup_doctype.report_drift > /tmp/drift.txt; cat /tmp/drift.txt | sed 's/^/   /'
! grep -Eq "^missing .*: [1-9]" /tmp/drift.txt || { echo "   DRIFT"; exit 1; }
$B bench --site "$SITE" execute luuvcrm.branch.check | sed 's/^/   branch.check: /'
n=$($B bench --site "$SITE" execute luuvcrm.api.get_menu_data | grep -o '"item_name"' | wc -l)
echo "   menu items: $n"; [ "$n" -gt 0 ] || exit 1
echo "   workers: $(for w in $WORKERS; do docker inspect -f '{{.State.Status}}' $w; done | tr '\n' ' ')"
EOF
  [ "$fail" = 0 ] || die "smoke failed — decide quickly: deploy/vps-deploy.sh rollback [--db]"
  echo "smoke ok — now the browser checks in DEPLOY_RUNBOOK.md §5 (a real shift on /pos, the KOT, /kitchen, /manager)"
}

# ─── rollback ────────────────────────────────────────────────────────────────
rollback() {
  say "rollback"
  [ -f "$STATE/last-deploy" ] || die "no deploy recorded in $STATE/last-deploy"
  local SNAPSHOT; SNAPSHOT=$(sed -n 's/^SNAPSHOT=//p' "$STATE/last-deploy")
  [ -n "$SNAPSHOT" ] || die "no snapshot path in $STATE/last-deploy"
  local withdb=${1:-}
  local STAMP=""
  if [ "$withdb" = "--db" ]; then
    [ -n "${DB_ROOT_PASSWORD:-}" ] || die "DB_ROOT_PASSWORD is needed to restore"
    STAMP=$(sed -n 's/^STAMP=//p' "$STATE/last-backup")
    [ -n "$STAMP" ] || die "no backup stamp in $STATE/last-backup"
    echo "database goes back to backup $STAMP — every sale since is gone with it"
  fi
  remote <<EOF
set -e
B="docker exec -w \$BENCH \$BACKEND"
\$B bench --site "\$SITE" set-maintenance-mode on
for c in \$BACKEND \$WORKERS; do
  docker exec "\$c" test -f "\$APP/luuvcrm/hooks.py" || continue
  docker cp "$SNAPSHOT" "\$c:/tmp/pre.tgz"
  docker exec -u root -e APP="\$APP" -e BENCH="\$BENCH" "\$c" bash -c '
    set -e; cd \$BENCH/apps
    mv luuvcrm "luuvcrm.failed-$TS"; tar xzf /tmp/pre.tgz; rm -f /tmp/pre.tgz
    chown -R 1001:1001 luuvcrm; echo "   \$HOSTNAME: app dir restored (failed tree kept as luuvcrm.failed-$TS)"'
done
if [ -n "$STAMP" ]; then
  \$B bench --site "\$SITE" restore "\$BENCH/sites/\$SITE/private/backups/$STAMP-\$SITE-database.sql.gz" --db-root-password "\$DB_ROOT_PASSWORD"
fi
docker restart "\$BACKEND" >/dev/null; sleep 15
for w in \$WORKERS; do docker restart "\$w" >/dev/null; done
\$B bench --site "\$SITE" clear-cache
\$B bench --site "\$SITE" set-maintenance-mode off
echo "ROLLBACK DONE"
EOF
  smoke || true
}

case "${1:-}" in
  preflight) preflight ;;
  backup)    backup ;;
  rehearse)  rehearse ;;
  deploy)    deploy ;;
  smoke)     smoke ;;
  rollback)  rollback "${2:-}" ;;
  *) sed -n '2,20p' "$0"; exit 2 ;;
esac
