import frappe

@frappe.whitelist(allow_guest=True, methods=["POST"])
def place_guest_order():
    try:
        data = frappe.local.form_dict
        
        customer_name = data.get("customer_name", "").strip()
        phone = data.get("phone", "").strip()
        email = data.get("email", "").strip()
        item_code = data.get("item_code", "").strip()
        item_name = data.get("item_name", "").strip()
        qty = int(data.get("qty", 1))
        rate = float(data.get("rate", 0))

        if not customer_name or not phone:
            return {
                "success": False,
                "error": "Customer name and phone are required"
            }

        # Create a Lead
        lead_data = {
            "doctype": "Lead",
            "lead_name": customer_name,
            "mobile_no": phone,
        }
        if email and "@" in email:
            lead_data["email_id"] = email
        lead = frappe.get_doc(lead_data)
        lead.flags.ignore_permissions = True
        lead.insert()
        
        # Create Quotation
        company = frappe.db.get_value("Company", {}, "name") or "Luuv Fryxo"
        price_list = frappe.db.get_value("Price List", {"enabled": 1, "selling": 1}, "name")
        quotation = frappe.get_doc({
            "doctype": "Quotation",
            "party_name": lead.name,
            "quotation_to": "Lead",
            "company": company,
            "selling_price_list": price_list or "Luuv Fryxo-Main-Menu",
            "transaction_date": frappe.utils.nowdate(),
            "items": [{
                "item_code": item_code or "",
                "item_name": item_name or "",
                "qty": qty,
                "rate": rate,
                "uom": "Nos",
            }],
        })
        quotation.flags.ignore_permissions = True
        quotation.insert()
        
        frappe.db.commit()

        return {
            "success": True,
            "order_id": quotation.name,
            "lead_id": lead.name,
            "message": f"Order {quotation.name} placed successfully"
        }

    except Exception as e:
        frappe.db.rollback()
        return {
            "success": False,
            "error": str(e),
        }
