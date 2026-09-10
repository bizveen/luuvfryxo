"""The PWA manifests, built from settings rather than shipped as files.

A manifest carries the app's name, its colour and its home-screen icons — three
things that are the restaurant's, not ours. They were three static JSON files
naming Luuv Fryxo and `#F15207`, and their icon paths (`/manager-icon-192.png`)
resolved to nothing at all: 404 for every one, so "Add to Home screen" produced
an app with no icon on any of the three.

Frappe templates only `.html` and `.md` under `www/`, so a `.json` there cannot
read a setting. `frappe.response` with type `raw` can, and carries the right
content type with it — which a manifest does need.
"""

import json

import frappe

from luuvcrm import settings

#: What each PWA calls itself and where it starts. The name, colour and icons
#: come from settings; only these three lines are ours.
APPS = {
    "manager": {
        "suffix": "Manager", "short": "Manager", "scope": "/manager",
        "about": "Approve bill changes and watch today's sales and cash drawer.",
    },
    "purchases": {
        "suffix": "Purchases", "short": "Purchases", "scope": "/purchases",
        "about": "Enter vendor bills and see what is still owed.",
    },
    "pos": {
        "suffix": "POS", "short": "POS", "scope": "/pos",
        "about": "Take orders and payments at the counter.",
    },
    "waiter": {
        "suffix": "Waiter", "short": "Waiter", "scope": "/waiter",
        "about": "Take and amend orders from the floor.",
    },
    "kitchen": {
        "suffix": "Kitchen", "short": "Kitchen", "scope": "/kitchen",
        "about": "Watch the order queue and move food through the pass.",
    },
    # The one staff icon: it starts on the chooser and its scope is the whole
    # site, so moving from /admin to /pos stays inside the installed window
    # instead of dropping into a browser tab with an address bar.
    "staff": {
        "suffix": "Staff", "short": "Staff", "scope": "/", "start": "/staff",
        "orientation": "any",
        "about": "Every staff app you can open, behind one icon.",
    },
    "cus": {
        "suffix": "", "short": "Order", "scope": "/cus",
        "about": "Order, track your orders and collect rewards.",
    },
}


@frappe.whitelist(allow_guest=True)
def manifest(app="cus"):
    """The web app manifest for one of the PWAs.

    Guest-readable because a manifest is fetched before anyone signs in — the
    customer app is public, and the staff ones are installed from the login
    screen. It carries no data: a name, a colour and two icon URLs.
    """
    spec = APPS.get(app) or APPS["cus"]
    name = settings.get("brand_name")
    body = {
        "name": f"{name} {spec['suffix']}".strip(),
        "short_name": f"{settings.get('brand_short')} {spec['short']}".strip(),
        "description": spec["about"],
        "id": spec.get("start", spec["scope"]),
        "start_url": spec.get("start", spec["scope"]),
        "scope": spec["scope"],
        "display": "standalone",
        "orientation": spec.get("orientation", "portrait"),
        "background_color": "#FAF8F6",
        "theme_color": settings.get("brand_color"),
        "icons": [
            {"src": settings.get("brand_icon_192"), "sizes": "192x192",
             "type": "image/png", "purpose": "any maskable"},
            {"src": settings.get("brand_icon_512"), "sizes": "512x512",
             "type": "image/png", "purpose": "any maskable"},
        ],
    }
    frappe.local.response.update({
        # "download" is the key for as_raw() in Frappe's response type map —
        # not "raw", which is a KeyError and a 500. The name is about the
        # handler, not about what the browser is asked to do with it; that is
        # display_content_as below.
        "type": "download",
        "filename": f"{app}_manifest.json",
        # Inline, not an attachment: a browser asked for this to read it, not to
        # save it, and Content-Disposition: attachment makes it do neither.
        "display_content_as": "inline",
        "content_type": "application/manifest+json",
        "filecontent": json.dumps(body, indent=2).encode(),
    })
