"""Replace included Golden Sauce with Tomato Salsa on the six active KG fries.

Run from the fryxo site's bench environment. Only Item.description is changed;
Golden Sauce retains its existing paid add-on menu row and price.
"""
import json
from pathlib import Path

import frappe


BEFORE = {
    "KG-FRIES-CHICKEN": "Crispy chicken, creamy coleslaw, mozzarella cheese, Luuv Chef special Hot Sauce, White Sauce, Golden Sauce and Smoky Brown Sauce.",
    "KG-FRIES-FISH": "Crispy fish, creamy coleslaw, mozzarella cheese, Luuv Chef special Hot Sauce, White Sauce, Golden Sauce and Smoky Brown Sauce.",
    "KG-FRIES-PRAWN": "Tempura prawns, creamy coleslaw, mozzarella cheese, Luuv Chef special Hot Sauce, Smoky Brown Sauce, White Sauce and Golden Sauce.",
    "KG-FRIES-BEEF": "Spicy masala ground beef, garden mayo salad, mozzarella cheese, Luuv Chef special Hot Sauce, Smoky Brown Sauce, White Sauce and Golden Sauce.",
    "KG-FRIES-SAUSAGE": "Grilled bratwurst sausage, creamy salad, mozzarella cheese, Smoky Brown Sauce, Golden Sauce, White Sauce and Luuv Chef special Hot Sauce.",
    "KG-FRIES-MIX": "Crispy chicken, prawns and sausage with golden fries, creamy coleslaw, mozzarella cheese and signature sauces.",
}
AFTER = {code: text.replace("Golden Sauce", "Tomato Salsa") for code, text in BEFORE.items()}
AFTER["KG-FRIES-MIX"] = BEFORE["KG-FRIES-MIX"].replace(
    "mozzarella cheese and signature sauces.",
    "mozzarella cheese, Tomato Salsa and signature sauces.",
)


def run():
    from luuvcrm.api.menu import menu_items

    menus_before = {branch: menu_items(branch) for branch in ("Kurunegala", "Luuv Fryxo")}
    fries = {row["code"] for row in menus_before["Kurunegala"] if row["group"] == "Loaded Fries"}
    assert fries == set(BEFORE), "Active Loaded Fries menu changed; review before applying"
    golden = next(row for row in menus_before["Kurunegala"] if row["code"] == "KG-EXTRA-GOLDEN")
    assert golden["group"] == "Add-ons" and golden["rate"] > 0

    current = {}
    for code in sorted(BEFORE):
        doc = frappe.get_doc("Item", code, for_update=True)
        assert doc.description in (BEFORE[code], AFTER[code]), ("Description changed since review", code)
        current[code] = doc.description

    backup = Path(frappe.get_site_path("private", "backups", "kg-fries-sauces-20261111.json"))
    backup.parent.mkdir(parents=True, exist_ok=True)
    if not backup.exists():
        backup.write_text(json.dumps({"before": current, "after": AFTER, "menus": menus_before}, indent=2), encoding="utf-8")
    else:
        saved = json.loads(backup.read_text(encoding="utf-8"))
        assert saved["after"] == AFTER, "Existing backup belongs to a different change"

    changed = []
    for code, description in AFTER.items():
        if current[code] != description:
            frappe.db.set_value("Item", code, "description", description)
            frappe.clear_document_cache("Item", code)
            changed.append(code)

    menus_after = {branch: menu_items(branch) for branch in menus_before}
    expected = json.loads(json.dumps(menus_before))
    for row in expected["Kurunegala"]:
        if row["code"] in AFTER:
            row["description"] = AFTER[row["code"]]
    assert menus_after == expected, "Unexpected menu, price or other-branch changes"
    frappe.db.commit()
    print(json.dumps({"changed": changed, "descriptions": AFTER, "golden_addon_rate": golden["rate"], "backup": str(backup), "other_menu_data_unchanged": True}))


if __name__ == "__main__":
    frappe.init(site="fryxo")
    frappe.connect()
    frappe.set_user("Administrator")
    try:
        run()
    except Exception:
        frappe.db.rollback()
        raise
    finally:
        frappe.destroy()
