#!/usr/bin/env bash
# The acceptance test (ROADMAP Phase 3): build a site from nothing, install the
# app, and sell something — with no manual step of ours in between.
#
#   MARIADB_ROOT_PASSWORD=... ./scripts/acceptance.sh
#
# It creates a throwaway site, runs ERPNext's own setup wizard (the installer's
# step, not ours), then luuvcrm/tests/fresh_install.py, then drops the site
# again. Nothing it does touches an existing site. Takes a few minutes: most of
# it is ERPNext installing.
#
# Keep --keep-site to look around afterwards; the site is left for you to drop.
set -euo pipefail

SITE="${SITE:-acceptance-$(date +%H%M%S).localhost}"
BENCH="${BENCH:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin}"
KEEP=0
[[ "${1:-}" == "--keep-site" ]] && KEEP=1

if [[ -z "${MARIADB_ROOT_PASSWORD:-}" ]]; then
  echo "MARIADB_ROOT_PASSWORD is not set — bench new-site needs it to create a database." >&2
  echo "  MARIADB_ROOT_PASSWORD=... $0" >&2
  exit 2
fi

cd "$BENCH"
echo "── bench: $BENCH"
echo "── site:  $SITE  (throwaway)"

cleanup() {
  if [[ $KEEP -eq 1 ]]; then
    echo "── keeping $SITE — drop it with: bench drop-site $SITE --force --mariadb-root-password ..."
    return
  fi
  echo "── dropping $SITE"
  bench drop-site "$SITE" --force --no-backup \
    --mariadb-root-password "$MARIADB_ROOT_PASSWORD" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "── creating the site and installing the app (this is the slow part)"
bench new-site "$SITE" \
  --admin-password "$ADMIN_PASSWORD" \
  --mariadb-root-password "$MARIADB_ROOT_PASSWORD" \
  --install-app luuvcrm

# ERPNext's own onboarding — a company, a currency, a country. Every ERPNext
# install does this; it is the installer's step, not one of ours, and a sale
# cannot be raised without a Company. Doing it here rather than in the Python
# keeps the line clear: everything after this must work with no further help.
echo "── running ERPNext's setup wizard (the installer's own step)"
bench --site "$SITE" execute frappe.desk.page.setup_wizard.setup_wizard.setup_complete \
  --kwargs "{'args': {'language': 'English', 'country': 'Sri Lanka', 'timezone': 'Asia/Colombo', 'currency': 'LKR', 'company_name': 'Acceptance Restaurant', 'company_abbr': 'AR', 'chart_of_accounts': 'Standard', 'fy_start_date': '$(date +%Y)-01-01', 'fy_end_date': '$(date +%Y)-12-31'}}"

echo "── the acceptance test"
bench --site "$SITE" execute luuvcrm.tests.fresh_install.execute
