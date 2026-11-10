# Text.lk SMS OTP — host setup

Date: 2026-11-10
Target site: `fryxo`

The customer registration flow sends one SMS OTP through Text.lk. A new phone
number verifies once and then creates its account and PIN. Existing accounts
continue to sign in with their PIN; they are not asked for OTP again.

## Values required

- Text.lk API token — **secret; transfer separately, never paste it into this
  file, Git, an issue, or a deployment log**.
- Approved Text.lk Sender ID: `LUUV FRYXO`.

`TextLKDemo` is for local testing only and must not be enabled on the host.

## Configure the production site

Run these commands from `/home/frappe/frappe-bench` in the backend container.
The first command reads the token without displaying it or storing it in shell
history:

```bash
read -rsp "Text.lk API token: " LUUV_TEXTLK_TOKEN
bench --site fryxo set-config luuvcrm_textlk_api_token "$LUUV_TEXTLK_TOKEN"
unset LUUV_TEXTLK_TOKEN

bench --site fryxo set-config luuvcrm_textlk_sender_id 'LUUV FRYXO'
bench --site fryxo set-config luuvcrm_textlk_allow_demo 0
bench --site fryxo clear-cache
bench --site fryxo clear-website-cache
```

The API token is stored only in the host's `sites/fryxo/site_config.json`. It is
not stored in the LuuvCRM database or repository.

For the complete code-deploy, database-update, configuration and smoke-test
order, follow [`RELEASE_2026-11-10.md`](RELEASE_2026-11-10.md).

## Verify without exposing the token

```bash
bench --site fryxo execute luuvcrm.api.sms.textlk_configured
```

Expected output:

```text
true
```

Then open `/cus` with a phone number that does not already have a Customer
Account. Confirm that:

1. The SMS arrives from the approved Sender ID.
2. The four-digit code verifies successfully.
3. The customer can set a PIN and create the account.
4. Signing in again asks only for the PIN, not another OTP.

## Safer handoff

Pass this Markdown file to the host operator, but send the API token through a
separate secure channel or enter it yourself during deployment. Do not send the
token in WhatsApp groups, email threads, screenshots, or source-control files.

If the host has a secret manager, it can be used instead of manually sharing
the token; the final value still needs to be written to
`luuvcrm_textlk_api_token` in the site's runtime configuration.

## Troubleshooting

- `textlk_configured` returns `false`: token or Sender ID is missing, or the
  production site was incorrectly left on `TextLKDemo`.
- Text.lk shows `Delivered` but the phone does not show the message: check the
  phone's Spam & blocked folder and ask Text.lk support to trace the message UID.
- The API succeeds but `/cus` still shows the old behavior: clear both caches
  and restart the backend after deploying the new Python code.
