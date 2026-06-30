import frappe

# Mapping: item_group (or item name pattern) -> kitchen_group name
ITEM_GROUP_MAP = {
    "Kottu": "Kottu",
    "Cheese Kottu": "Kottu",
    "Signature Kottu": "Kottu",
    "Fried Rice": "Rice & Noodles",
    "Rice & Curry": "Rice & Noodles",
    "Biryani & Naan Combos": "Biryani",
    "Biryani": "Biryani",
    "Pizza": "Pizza",
    "Grills": "Grills & Sizzlers",
    "Sizzler Platters": "Grills & Sizzlers",
    "North Asian": "Rice & Noodles",
    "Sri Lankan Fusion": "Rice & Noodles",
    "Indian Cuisine": "Curries & Gravies",
    "European Dishes & Salads": "Sides & Starters",
    "Soups": "Soups & Salads",
    "Sides & Accompaniments": "Sides & Starters",
    "Vegetarian": "Curries & Gravies",
    "Coffee & Tea": "Coffee & Tea",
    "Beverages": "Beverages",
    "Desserts": "Desserts",
}

frappe.init("fryxo")
frappe.connect()

# Get all items
items = frappe.get_all("Item", fields=["name", "item_group"])
assigned = 0
for item in items:
    grp = item.item_group or ""
    kg_name = ITEM_GROUP_MAP.get(grp, "Other")
    kg = frappe.db.get_value("Kitchen Group", {"group_name": kg_name}, "name")
    if kg:
        frappe.db.set_value("Item", item.name, "kitchen_group", kg)
        assigned += 1

frappe.db.commit()
print(f"Assigned {assigned} items to kitchen groups")
frappe.destroy()
