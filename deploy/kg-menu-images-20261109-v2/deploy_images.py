"""Install the versioned Kurunegala menu images without touching old assets.

Run from the bench directory with, for example:

    SITE=test.localhost ./env/bin/python \
      apps/luuvcrm/deploy/kg-menu-images-20261109-v2/deploy_images.py

The generated source PNGs remain in this versioned folder. Public copies use
the same versioned names, which makes browser caching and rollback predictable.
"""

import hashlib
import json
import os
import shutil
from pathlib import Path

import frappe


SITE = os.environ.get("SITE", "fryxo")
RESTAURANT = "Kurunegala"
SOURCE_DIR = Path(__file__).resolve().parent
BENCH_DIR = SOURCE_DIR.parents[3]

ITEM_IMAGES = {
    "KG-FRIES-CHICKEN": "kg-fries-chicken-v2.png",
    "KG-FRIES-FISH": "kg-fries-fish-v2.png",
    "KG-FRIES-PRAWN": "kg-fries-prawn-v2.png",
    "KG-FRIES-BEEF": "kg-fries-beef-v2.png",
    "KG-FRIES-SAUSAGE": "kg-fries-sausage-v2.png",
    "KG-FRIES-MIX": "kg-fries-mix-v2.png",
    "KG-CHIPS-FISH": "kg-chips-fish-v2.png",
    "KG-CHIPS-WIENILET": "kg-chips-wienilet-v2.png",
    "KG-CHIPS-SPICY": "kg-chips-spicy-v2.png",
    "KG-THAI-BLUEBERRY": "kg-thai-blueberry-v2.png",
    "KG-THAI-STRAWBERRY": "kg-thai-strawberry-v2.png",
    "KG-THAI-MANGO": "kg-thai-mango-v2.png",
    "KG-SMOOTHIE-MANGO": "kg-smoothie-mango-v2.png",
    "KG-SMOOTHIE-BLUEBERRY": "kg-smoothie-blueberry-v2.png",
    "KG-SMOOTHIE-PASSION": "kg-smoothie-passion-v2.png",
    "KG-SMOOTHIE-STRAWBERRY": "kg-smoothie-strawberry-v2.png",
    "KG-ICED-COFFEE": "kg-iced-coffee-v2.png",
    "KG-ICED-CHOCOLATE": "kg-iced-chocolate-v2.png",
    "KG-LEMONADE-BLUEBERRY": "kg-lemonade-blueberry-v2.png",
    "KG-LEMONADE-STRAWBERRY": "kg-lemonade-strawberry-v2.png",
    "KG-LEMONADE-PASSION": "kg-lemonade-passion-v2.png",
    "KG-LEMON-MINT": "kg-lemon-mint-v2.png",
    "KG-EXTRA-BROWN": "kg-extra-brown-v2.png",
    "KG-EXTRA-WHITE": "kg-extra-white-v2.png",
    "KG-EXTRA-GOLDEN": "kg-extra-golden-v2.png",
    "KG-EXTRA-SALSA": "kg-extra-salsa-v2.png",
    "KG-EXTRA-HOT": "kg-extra-hot-v2.png",
    "KG-EXTRA-CHEESE": "kg-extra-cheese-v2.png",
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


os.chdir(os.environ.get("SITES_PATH", str(BENCH_DIR / "sites")))
frappe.init(site=SITE)
frappe.connect()
frappe.set_user("Administrator")

try:
    assert len(ITEM_IMAGES) == 28
    missing_items = [code for code in ITEM_IMAGES if not frappe.db.exists("Item", code)]
    assert not missing_items, f"Missing Item records: {missing_items}"

    public_dir = Path(frappe.get_site_path("public", "files"))
    public_dir.mkdir(parents=True, exist_ok=True)
    backup_path = Path(
        frappe.get_site_path(
            "private", "backups", "kg-menu-images-before-20261109-v2.json"
        )
    )
    previous = {
        code: frappe.db.get_value("Item", code, "image") for code in ITEM_IMAGES
    }
    if not backup_path.exists():
        backup_path.write_text(
            json.dumps(
                {"restaurant": RESTAURANT, "site": SITE, "images": previous},
                indent=2,
                sort_keys=True,
            )
        )

    installed = {}
    for code, filename in ITEM_IMAGES.items():
        source = SOURCE_DIR / filename
        assert source.is_file(), source
        destination = public_dir / filename
        shutil.copy2(source, destination)
        assert digest(source) == digest(destination), filename
        url = f"/files/{filename}"
        frappe.db.set_value("Item", code, "image", url, update_modified=False)
        installed[code] = url

    frappe.db.commit()
    frappe.clear_cache()
    from frappe.website.utils import clear_website_cache

    clear_website_cache()

    actual = {
        code: frappe.db.get_value("Item", code, "image") for code in ITEM_IMAGES
    }
    assert actual == installed
    print(
        json.dumps(
            {
                "ok": True,
                "site": SITE,
                "restaurant": RESTAURANT,
                "installed": len(installed),
                "backup": str(backup_path),
            },
            sort_keys=True,
        )
    )
except Exception:
    frappe.db.rollback()
    raise
finally:
    frappe.destroy()
