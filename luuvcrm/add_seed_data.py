import frappe

def run():
    # 1. Add 5 Restaurant Tables
    tables = ["Table-1", "Table-2", "Table-3", "Table-4", "Table-5"]
    for t in tables:
        if not frappe.db.exists("Restaurant Table", t):
            doc = frappe.get_doc({
                "doctype": "Restaurant Table",
                "table_name": t
            })
            doc.insert(ignore_permissions=True)
            print(f"Created table: {t}")
        else:
            print(f"Table {t} already exists")

    # 2. Add 10 Food Items
    foods = [
        ("FOOD-001", "Chicken Burger", "Food", 850),
        ("FOOD-002", "French Fries", "Food", 400),
        ("FOOD-003", "Club Sandwich", "Food", 750),
        ("FOOD-004", "Pepperoni Pizza", "Food", 1600),
        ("FOOD-005", "Crispy Fried Chicken", "Food", 950),
        ("FOOD-006", "Cheesy Garlic Bread", "Food", 500),
        ("FOOD-007", "Pasta Carbonara", "Food", 1200),
        ("FOOD-008", "Beef Burger", "Food", 1000),
        ("FOOD-009", "Veggie Wrap", "Food", 650),
        ("FOOD-010", "Hot Wings", "Food", 700),
    ]

    # 3. Add 5 Beverage Items
    beverages = [
        ("BEV-001", "Iced Coffee", "Beverage", 450),
        ("BEV-002", "Fresh Lemonade", "Beverage", 350),
        ("BEV-003", "Coca-Cola", "Beverage", 250),
        ("BEV-004", "Mango Smoothie", "Beverage", 600),
        ("BEV-005", "Vanilla Milkshake", "Beverage", 550),
    ]

    all_items = foods + beverages

    for code, name, group, rate in all_items:
        # Ensure Item Group exists
        if not frappe.db.exists("Item Group", group):
            ig = frappe.get_doc({
                "doctype": "Item Group",
                "item_group_name": group,
                "parent_item_group": "All Item Groups"
            })
            ig.insert(ignore_permissions=True)

        if not frappe.db.exists("Item", code):
            item_doc = frappe.get_doc({
                "doctype": "Item",
                "item_code": code,
                "item_name": name,
                "item_group": group,
                "stock_uom": "Nos",
                "is_stock_item": 0,
                "standard_rate": rate,
                "valuation_rate": rate
            })
            item_doc.insert(ignore_permissions=True)
            print(f"Created item: {code} - {name} ({group}) @ {rate}")
        else:
            # Update standard rate if needed
            frappe.db.set_value("Item", code, "standard_rate", rate)
            print(f"Item {code} already exists, updated rate to {rate}")

    frappe.db.commit()
    print("Seed data successfully added!")
