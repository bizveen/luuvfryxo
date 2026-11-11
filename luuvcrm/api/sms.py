"""Transactional SMS providers used by customer verification."""

import frappe
import requests

from luuvcrm import settings


TEXTLK_SEND_URL = "https://app.text.lk/api/v3/sms/send"


def textlk_configured():
    """True only when both secrets needed for a real Text.lk send exist."""
    sender_id = str(settings.get("textlk_sender_id") or "").strip()
    demo_allowed = str(settings.get("textlk_allow_demo") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    return bool(
        str(settings.get("textlk_api_token") or "").strip()
        and sender_id
        # Text.lk explicitly forbids its demo identity for production OTPs.
        and (sender_id.lower() != "textlkdemo" or demo_allowed)
    )


def send_textlk_sms(recipient, message):
    """Send one plain SMS through Text.lk's Bearer-authenticated JSON API."""
    token = str(settings.get("textlk_api_token") or "").strip()
    sender_id = str(settings.get("textlk_sender_id") or "").strip()
    if not textlk_configured():
        return False

    try:
        response = requests.post(
            TEXTLK_SEND_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json={
                "recipient": str(recipient or "").strip(),
                "sender_id": sender_id,
                "type": "plain",
                "message": str(message or "").strip(),
            },
            timeout=10,
        )
        payload = response.json() if response.content else {}
        status = payload.get("status")
        accepted = status is True or str(status or "").lower() == "success"
        if response.ok and accepted:
            return True
        # Provider errors are useful operationally, but never log the request,
        # headers or API token. Their response message contains only the public
        # validation/account reason (sender not approved, no credit, etc.).
        provider_message = str(payload.get("message") or "request rejected")[:240]
        frappe.log_error(
            f"Text.lk rejected SMS: HTTP {response.status_code}; {provider_message}",
            "Text.lk SMS",
        )
        return False
    except Exception as exc:
        # Never include the request body or token in the error log.
        frappe.log_error(f"Text.lk SMS error: {exc}", "Text.lk SMS")
        return False
