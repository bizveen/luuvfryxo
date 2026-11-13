"""Firebase Cloud Messaging for the native customer app.

The Flutter shell registers an FCM token only after the customer has signed in.
The public endpoints below authenticate with that same seven-day customer token;
the raw FCM token never becomes a customer identifier and is never written to a
log.  Sending uses FCM HTTP v1 with a service account kept in the site's private
configuration, outside this repository.
"""

from __future__ import annotations

import hashlib
import os
from urllib.parse import urlencode

import frappe
import requests
from frappe import _
from frappe.utils import now_datetime

from luuvcrm import branch, roles
from luuvcrm.api.customers import _mobile_from_token


PUSH_DOCTYPE = "Customer Push Token"
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
FCM_TIMEOUT_SECONDS = 12
VALID_PLATFORMS = {"android": "Android", "ios": "iOS"}
VALID_BROADCAST_KINDS = {"offer", "promotion"}


def _value(explicit, key):
    return explicit if explicit is not None else frappe.local.form_dict.get(key)


def _token_hash(device_token):
    return hashlib.sha256(device_token.encode("utf-8")).hexdigest()


def _clean_device_token(value):
    token = str(value or "").strip()
    if len(token) < 32 or len(token) > 4096 or any(ch.isspace() for ch in token):
        return ""
    return token


@frappe.whitelist(allow_guest=True, methods=["POST"])
def customer_register_push_token(customer_token=None, device_token=None, platform=None,
                                 restaurant=None, app_version=None):
    """Attach one native installation to the signed-in customer."""
    customer_token = str(_value(customer_token, "customer_token") or "").strip()
    mobile = _mobile_from_token(customer_token)
    if not mobile:
        return {"success": False, "error": "Login required"}
    if not frappe.db.exists("DocType", PUSH_DOCTYPE):
        return {"success": False, "error": "Push notifications need bench migrate"}

    device_token = _clean_device_token(_value(device_token, "device_token"))
    if not device_token:
        return {"success": False, "error": "Invalid device token"}
    platform_key = str(_value(platform, "platform") or "").strip().lower()
    platform_label = VALID_PLATFORMS.get(platform_key)
    if not platform_label:
        return {"success": False, "error": "Platform must be Android or iOS"}

    restaurant = str(_value(restaurant, "restaurant") or "").strip()
    if restaurant and not frappe.db.exists("Restaurant", restaurant):
        return {"success": False, "error": "Restaurant not found"}

    digest = _token_hash(device_token)
    values = {
        "device_token": device_token,
        "mobile": mobile,
        "restaurant": restaurant,
        "platform": platform_label,
        "app_version": str(_value(app_version, "app_version") or "")[:40],
        "enabled": 1,
        "last_seen": now_datetime(),
    }
    if frappe.db.exists(PUSH_DOCTYPE, digest):
        frappe.db.set_value(PUSH_DOCTYPE, digest, values)
    else:
        frappe.get_doc({
            "doctype": PUSH_DOCTYPE,
            "token_hash": digest,
            **values,
        }).insert(ignore_permissions=True)
    return {"success": True, "registered": True}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def customer_unregister_push_token(customer_token=None, device_token=None):
    """Disable this installation when the WebView customer signs out."""
    customer_token = str(_value(customer_token, "customer_token") or "").strip()
    mobile = _mobile_from_token(customer_token)
    if not mobile:
        return {"success": False, "error": "Login required"}
    device_token = _clean_device_token(_value(device_token, "device_token"))
    if not device_token or not frappe.db.exists("DocType", PUSH_DOCTYPE):
        return {"success": True, "registered": False}
    digest = _token_hash(device_token)
    owner = frappe.db.get_value(PUSH_DOCTYPE, digest, "mobile")
    if owner == mobile:
        frappe.db.set_value(PUSH_DOCTYPE, digest,
                            {"enabled": 0, "last_seen": now_datetime()})
    return {"success": True, "registered": False}


def _service_account_path():
    configured = str(frappe.conf.get("firebase_service_account_file") or "").strip()
    if not configured:
        return ""
    return configured if os.path.isabs(configured) else frappe.get_site_path(configured)


def _fcm_context():
    """Return (OAuth bearer token, Firebase project id)."""
    from google.auth import default
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    path = _service_account_path()
    if path:
        if not os.path.isfile(path):
            raise RuntimeError("firebase_service_account_file does not exist")
        credentials = service_account.Credentials.from_service_account_file(
            path, scopes=[FCM_SCOPE])
        project_id = credentials.project_id
    else:
        credentials, project_id = default(scopes=[FCM_SCOPE])
    project_id = str(frappe.conf.get("firebase_project_id") or project_id or "").strip()
    if not project_id:
        raise RuntimeError("firebase_project_id is not configured")
    credentials.refresh(Request())
    return credentials.token, project_id


def _push_data(data):
    return {str(key): str(value) for key, value in (data or {}).items()
            if value is not None}


def _disable_token(token_hash):
    if frappe.db.exists(PUSH_DOCTYPE, token_hash):
        frappe.db.set_value(PUSH_DOCTYPE, token_hash, "enabled", 0)


def _send_one(device, title, body, data, fcm_context):
    access_token, project_id = fcm_context
    kind = data.get("type") or "promotion"
    channel = "order_updates" if kind == "order_ready" else "offers_promotions"
    try:
        response = requests.post(
            f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json={
                "message": {
                    "token": device.device_token,
                    "notification": {"title": title, "body": body},
                    "data": _push_data(data),
                    "android": {
                        "priority": "HIGH" if kind == "order_ready" else "NORMAL",
                        "notification": {
                            "channel_id": channel,
                            "sound": "default",
                            "click_action": "FLUTTER_NOTIFICATION_CLICK",
                        },
                    },
                    "apns": {
                        "payload": {"aps": {"sound": "default", "badge": 1}},
                    },
                }
            },
            timeout=FCM_TIMEOUT_SECONDS,
        )
    except requests.RequestException as error:
        # Exception text can include the request, including its credentials.
        # One failed device must not prevent delivery to the remaining ones.
        frappe.log_error(
            title="Customer push notification failed",
            message=f"FCM transport {type(error).__name__}; token hash {device.name}",
        )
        return False
    if response.ok:
        return True

    # FCM explicitly says these tokens can never become valid again. Disable
    # them so every future promotion does not keep retrying dead installs.
    error_text = response.text or ""
    if response.status_code in (400, 404) and any(
            code in error_text for code in (
                "UNREGISTERED", "registration-token-not-registered")):
        _disable_token(device.name)
    frappe.log_error(
        title="Customer push notification failed",
        message=f"FCM {response.status_code}; token hash {device.name}",
    )
    return False


def _enabled_devices(filters=None):
    wanted = {"enabled": 1, **(filters or {})}
    return frappe.get_all(PUSH_DOCTYPE, filters=wanted,
                          fields=["name", "device_token", "mobile", "restaurant"],
                          limit_page_length=10000)


def send_customer_push(mobile, title, body, data=None):
    devices = _enabled_devices({"mobile": mobile})
    if not devices:
        return {"sent": 0, "failed": 0}
    try:
        context = _fcm_context()
    except Exception as error:
        frappe.log_error(title="Firebase push is not configured",
                         message=f"FCM credential setup failed ({type(error).__name__})")
        return {"sent": 0, "failed": len(devices)}
    sent = sum(bool(_send_one(device, title, body, data or {}, context))
               for device in devices)
    return {"sent": sent, "failed": len(devices) - sent}


def send_order_ready_push(order_name):
    order = frappe.db.get_value(
        "POS Order", order_name,
        ["name", "mobile", "restaurant", "restaurant_table"], as_dict=True)
    if not order or not order.mobile:
        return {"sent": 0, "failed": 0}
    restaurant = order.restaurant or ""
    query = urlencode({"r": restaurant, "tab": "orders"}) if restaurant else urlencode({"tab": "orders"})
    return send_customer_push(
        order.mobile,
        "Your order is ready! 🍽️",
        f"#{order.name} is prepared and ready for pickup"
        + (f" at {restaurant}." if restaurant else "."),
        {
            "type": "order_ready",
            "order_name": order.name,
            "restaurant": restaurant,
            "route": f"/cus?{query}",
        },
    )


def queue_order_ready_push(order_name):
    """Queue after commit so a kitchen tap never waits on Google's network."""
    if not frappe.db.exists("DocType", PUSH_DOCTYPE):
        return False
    mobile = frappe.db.get_value("POS Order", order_name, "mobile")
    if not mobile or not frappe.db.exists(
            PUSH_DOCTYPE, {"mobile": mobile, "enabled": 1}):
        return False
    frappe.enqueue(
        "luuvcrm.api.push.send_order_ready_push",
        queue="short",
        enqueue_after_commit=True,
        order_name=order_name,
    )
    return True


@frappe.whitelist(methods=["POST"])
def queue_customer_push_broadcast(kind=None, title=None, body=None, restaurant=None):
    """Queue an owner/manager announcement for offers or promotions."""
    roles.require_manager()
    kind = str(_value(kind, "kind") or "").strip().lower()
    title = str(_value(title, "title") or "").strip()[:100]
    body = str(_value(body, "body") or "").strip()[:300]
    restaurant = str(_value(restaurant, "restaurant") or "").strip()
    if kind not in VALID_BROADCAST_KINDS:
        frappe.throw(_("Notification type must be offer or promotion"))
    if not title or not body:
        frappe.throw(_("Title and message are required"))
    if restaurant and not frappe.db.exists("Restaurant", restaurant):
        frappe.throw(_("Restaurant not found"))
    if not roles.is_owner():
        allowed = branch.available_restaurants()
        restaurant = restaurant or branch.viewing() or ""
        if not restaurant or restaurant not in allowed:
            frappe.throw(_("You are not permitted that branch"),
                         frappe.PermissionError)

    filters = {"restaurant": restaurant} if restaurant else {}
    recipients = len(_enabled_devices(filters))
    frappe.enqueue(
        "luuvcrm.api.push.send_customer_push_broadcast",
        queue="short",
        enqueue_after_commit=True,
        kind=kind,
        title=title,
        body=body,
        restaurant=restaurant,
    )
    return {"ok": True, "queued": recipients, "restaurant": restaurant or "All"}


def send_customer_push_broadcast(kind, title, body, restaurant=""):
    filters = {"restaurant": restaurant} if restaurant else {}
    devices = _enabled_devices(filters)
    if not devices:
        return {"sent": 0, "failed": 0}
    try:
        context = _fcm_context()
    except Exception as error:
        frappe.log_error(title="Firebase push is not configured",
                         message=f"FCM credential setup failed ({type(error).__name__})")
        return {"sent": 0, "failed": len(devices)}
    query = {"tab": "offers"}
    if restaurant:
        query["r"] = restaurant
    data = {
        "type": kind,
        "restaurant": restaurant,
        "route": "/cus?" + urlencode(query),
    }
    sent = sum(bool(_send_one(device, title, body, data, context))
               for device in devices)
    return {"sent": sent, "failed": len(devices) - sent}
