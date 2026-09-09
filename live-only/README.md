# live-only — code the live host runs that is not this product's

Nothing in here is imported by the restaurant app, tested by its suite, or
installed by `bench install-app`. It exists because the live bench installs
`luuvcrm` on **two** sites — the restaurant (`fryxo`) and a clothing shop
(`clothing`, "Zeloura") — and the shop's server-side code was written into
this app's package on that host. The refactor deleted it from the repo
(commit c4d4688, "the other product"), which was right for the product and
wrong for the host: the shop's ten theme pages POST to
`luuvcrm.zeloura_api.get_csrf`, and `/network` and `/shop` are served from
its `www/`.

`deploy/vps-deploy.sh` copies `live-only/zeloura/luuvcrm/` over the package
on the host after every overlay, so a deploy never removes it. Every file
here is byte-identical to the host's copy as of 2026-09-09 (checked by md5 in
the script's `preflight`). If the host's copy ever differs, the host is
right: pull it here first.

It lives outside `luuvcrm/` on purpose. The suite scans the package for
runtime `create_custom_field` calls, role names in page modules and pages
that name one client; this code would trip all three, and none of those
rules is meant for it.

| File | Why the shop needs it |
|---|---|
| `zeloura_api.py` | `get_csrf`, `get_page_data`, `get_themes`, `get_landing` — the theme pages' endpoints (guest) |
| `zeloura_setup.py` | builds the Zeloura DocTypes (`Zeloura Theme`, `Zeloura Product`, MLM settings…) — they are `custom: 1` in module **Zeloura**, which this app owns |
| `place_order_api.py` | `place_guest_order`, a guest checkout endpoint |
| `populate*.py`, `*_theme_*.py` | one-off seeders for themes and settings; kept so a re-run is possible |
| `www/network.html`, `www/shop.html` | the shop's pages (both answer 200 on the clothing site) |

The right home for all of this is an app of its own. Until then, the clothing
site carries `luuvcrm_passive: 1` in its site_config so this app's request
hooks and patches stay out of it — see `settings.KEYS` and hooks.py.
