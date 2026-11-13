"""Firebase Cloud Messaging for the native customer app.

Signed-in installations are attached to the customer's seven-day session.
Guest installations can be attached to one order using a server-signed ticket
returned by the order-placement response.  A bare phone number is never enough
to register a device, and guest devices never receive marketing broadcasts.
The raw FCM token is never used as a customer identifier or written to a log.
"""

from __future__ import annotations

import hashlib
import hmac
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
VALID_PLATFORMS = {"android": "Android", "ios": "iOS", "web": "Web"}
VALID_BROADCAST_KINDS = {"offer", "promotion"}
CUSTOMER_SCOPE = "Customer"
GUEST_ORDER_SCOPE = "Guest Order"


def _value(explicit, key):
    return explicit if explicit is not None else frappe.local.form_dict.get(key)


def _token_hash(device_token):
    return hashlib.sha256(device_token.encode("utf-8")).hexdigest()


def _guest_token_hash(order_name, device_token):
    value = f"guest-order:{order_name}:{device_token}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def guest_order_push_ticket(order_name):
    """Unforgeable proof returned only to the client that placed this order."""
    from frappe.utils.password import get_encryption_key

    value = f"guest-order-push:{order_name}".encode("utf-8")
    return hmac.new(get_encryption_key().encode(), value,
                    hashlib.sha256).hexdigest()[:32]


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
        return {"success": False, "error": "Platform must be Android, iOS or Web"}

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
        "token_scope": CUSTOMER_SCOPE,
        "order_name": "",
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
def customer_register_guest_order_push(order_name=None, guest_ticket=None,
                                       device_token=None, platform=None,
                                       app_version=None):
    """Attach this native installation to one CUS order, without a login.

    The signed ticket is returned in the HTTPS response that created the order.
    It prevents somebody who merely knows a sequential POS order name or phone
    number from registering their device for another customer's notification.
    """
    order_name = str(_value(order_name, "order_name") or "").strip()
    guest_ticket = str(_value(guest_ticket, "guest_ticket") or "").strip()
    expected = guest_order_push_ticket(order_name) if order_name else ""
    if not expected or not hmac.compare_digest(guest_ticket, expected):
        return {"success": False, "error": "Invalid order ticket"}
    if not frappe.db.exists("DocType", PUSH_DOCTYPE):
        return {"success": False, "error": "Push notifications need bench migrate"}

    order = frappe.db.get_value(
        "POS Order", order_name,
        ["name", "mobile", "restaurant", "waiter_name", "order_source",
         "kitchen_status"], as_dict=True,
    )
    if (not order or not order.mobile or order.order_source != "Online"
            or str(order.waiter_name or "").strip().upper() != "CUS"
            or order.kitchen_status in ("Served", "Cancelled")):
        return {"success": False, "error": "Order is not available"}

    device_token = _clean_device_token(_value(device_token, "device_token"))
    if not device_token:
        return {"success": False, "error": "Invalid device token"}
    platform_key = str(_value(platform, "platform") or "").strip().lower()
    platform_label = VALID_PLATFORMS.get(platform_key)
    if not platform_label:
        return {"success": False, "error": "Platform must be Android, iOS or Web"}

    digest = _guest_token_hash(order.name, device_token)
    values = {
        "device_token": device_token,
        "mobile": order.mobile,
        "restaurant": order.restaurant or "",
        "platform": platform_label,
        "app_version": str(_value(app_version, "app_version") or "")[:40],
        "token_scope": GUEST_ORDER_SCOPE,
        "order_name": order.name,
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
    return {"success": True, "registered": True, "order_name": order.name}


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
    is_order_update = kind in {"order_confirmed", "order_ready"}
    channel = "order_updates" if is_order_update else "offers_promotions"
    message = {
        "token": device.device_token,
        "notification": {"title": title, "body": body},
        "data": _push_data(data),
        "android": {
            "priority": "HIGH" if is_order_update else "NORMAL",
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
    if device.get("platform") == "Web":
        route = str(data.get("route") or "/cus?tab=orders")
        web_link = (route if route.startswith(("http://", "https://"))
                    else frappe.utils.get_url(route))
        message["webpush"] = {
            "headers": {"Urgency": "high" if is_order_update else "normal"},
        }
        # FCM only accepts secure click/icon URLs. Local HTTP development still
        # receives the notification; cus_sw.js routes the click from data.route.
        if web_link.startswith("https://"):
            icon = frappe.utils.get_url("/assets/luuvcrm/app/icon-192.png")
            message["webpush"].update({
                "notification": {"icon": icon, "badge": icon},
                "fcm_options": {"link": web_link},
            })
    try:
        response = requests.post(
            f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json={"message": message},
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
    wanted = {"enabled": 1, "token_scope": CUSTOMER_SCOPE, **(filters or {})}
    return frappe.get_all(PUSH_DOCTYPE, filters=wanted,
                          fields=["name", "device_token", "mobile", "restaurant",
                                  "platform", "token_scope", "order_name"],
                          limit_page_length=10000)


def _order_ready_devices(order_name, mobile):
    """Signed-in devices for the mobile plus guest devices for this order."""
    devices = _enabled_devices({"mobile": mobile})
    devices.extend(_enabled_devices({
        "token_scope": GUEST_ORDER_SCOPE,
        "order_name": order_name,
    }))
    # A signed-in customer may have placed the order before logging in. Avoid a
    # duplicate notification if the same FCM installation exists in both sets.
    unique = {}
    for device in devices:
        unique.setdefault(device.device_token, device)
    return list(unique.values())


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
    devices = _order_ready_devices(order.name, order.mobile)
    if not devices:
        return {"sent": 0, "failed": 0}
    try:
        context = _fcm_context()
    except Exception as error:
        frappe.log_error(title="Firebase push is not configured",
                         message=f"FCM credential setup failed ({type(error).__name__})")
        return {"sent": 0, "failed": len(devices)}
    data = {
        "type": "order_ready",
        "order_name": order.name,
        "restaurant": restaurant,
        "route": f"/cus?{query}",
    }
    title = "Your order is ready! 🍽️"
    body = (f"#{order.name} is prepared and ready for pickup"
            + (f" at {restaurant}." if restaurant else "."))
    sent = sum(bool(_send_one(device, title, body, data, context))
               for device in devices)
    return {"sent": sent, "failed": len(devices) - sent}


def send_order_confirmed_push(order_name):
    """Tell the installation that placed an APP order it was accepted."""
    order = frappe.db.get_value(
        "POS Order", order_name,
        ["name", "mobile", "restaurant"], as_dict=True)
    if not order or not order.mobile:
        return {"sent": 0, "failed": 0}
    restaurant = order.restaurant or ""
    query = urlencode({"r": restaurant, "tab": "orders"}) if restaurant else urlencode({"tab": "orders"})
    devices = _order_ready_devices(order.name, order.mobile)
    if not devices:
        return {"sent": 0, "failed": 0}
    try:
        context = _fcm_context()
    except Exception as error:
        frappe.log_error(title="Firebase push is not configured",
                         message=f"FCM credential setup failed ({type(error).__name__})")
        return {"sent": 0, "failed": len(devices)}
    data = {
        "type": "order_confirmed",
        "order_name": order.name,
        "restaurant": restaurant,
        "status": "Pending",
        "route": f"/cus?{query}",
    }
    title = "Order confirmed ✅"
    body = (f"#{order.name} was confirmed"
            + (f" by {restaurant}." if restaurant else ".")
            + " We will start preparing it shortly.")
    sent = sum(bool(_send_one(device, title, body, data, context))
               for device in devices)
    return {"sent": sent, "failed": len(devices) - sent}


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


def queue_order_confirmed_push(order_name):
    """Queue confirmation after confirm_app_order has made its explicit commit."""
    if not frappe.db.exists("DocType", PUSH_DOCTYPE):
        return False
    mobile = frappe.db.get_value("POS Order", order_name, "mobile")
    if not mobile or not frappe.db.exists(
            PUSH_DOCTYPE, {"mobile": mobile, "enabled": 1}):
        return False
    frappe.enqueue(
        "luuvcrm.api.push.send_order_confirmed_push",
        queue="short",
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
