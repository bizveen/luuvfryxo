import frappe

def test():
    frappe.init(site="clothing")
    frappe.connect()
    try:
        d = frappe.new_doc("Zeloura Product")
        d.parent = "test"
        d.parenttype = "Zeloura Theme"
        d.parentfield = "featured_items"
        d.item = "TEST-001"
        d.title = "Test Product"
        d.price = 100
        d.insert()
        frappe.db.commit()
        print("OK - child created with Zeloura Theme as parent")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        frappe.db.rollback()
        frappe.destroy()
