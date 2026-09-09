import frappe

def run():
    parent = "Zeloura Settings"
    
    # Use set_single_value for scalar fields
    scalar_fields = {
        "hero_title": "main character era",
        "hero_subtitle": "the drop you have been waiting for.",
        "hero_badge": "drop 05 just dropped ???",
        "hero_cta_text": "shop drop 05",
        "hero_cta_link": "/shop",
        "hero_secondary_text": "what's my vibe?",
        "hero_secondary_link": "#vibes",
        "hero_product_name": "the coquette set",
        "hero_product_price": 4500,
        "hero_product_image": "https://images.unsplash.com/photo-1515372039744-b8f02a3ae446?w=600&h=750&fit=crop&q=80",
        "featured_title": "find your fit",
        "featured_subtitle": "who are you this week",
        "bestsellers_title": "trending now",
        "bestsellers_subtitle": "bestsellers ss26",
        "values_title": "what we stand for",
        "values_subtitle": "values we dress in",
        "lookbook_title": "editorial",
        "lookbook_subtitle": "the lookbook ss26",
        "newsletter_title": "join the club",
        "newsletter_subtitle": "pink club",
        "newsletter_description": "get early access to drops, exclusive sales, and first dibs. no spam, just the good stuff.",
        "newsletter_cta": "join free",
        "about_subtitle": "our story",
        "about_title": "made for girls who run the day",
        "about_content": "zeloura collective isn't just clothing—it's a vibe, a mood, a lifestyle for the modern girl. every piece is designed to make you feel like the main character.",
        "about_image": "https://images.unsplash.com/photo-1483985988355-763728e1935b?w=600&h=400&fit=crop&q=80",
        "about_stat1_label": "happy besties",
        "about_stat1_value": "15k+",
    }
    for key, val in scalar_fields.items():
        frappe.db.set_single_value("Zeloura Settings", key, val)
    print("Scalar fields updated")

    # Clear existing child table data
    for table in ["tabZeloura Product", "tabZeloura Bestseller", "tabZeloura Value", "tabZeloura Lookbook"]:
        frappe.db.sql(f"DELETE FROM `{table}` WHERE `parent`=%s", parent)
    print("Child tables cleared")

    # Insert Values
    values = [
        ("????", "Inclusive Sizing", "size 00 to 22 — every body is a zeloura body.", 1),
        ("????", "Sustainable Materials", "eco-friendly fabrics that feel good on your skin and the planet.", 2),
        ("????", "Girls Supporting Girls", "we donate 5% of every purchase to girls' education.", 3),
        ("????", "Afterpay Available", "split your purchase into 4 interest-free payments.", 4),
    ]
    for emoji, title, desc, order in values:
        frappe.db.sql(
            "INSERT INTO `tabZeloura Value` (`name`,`parent`,`parentfield`,`parenttype`,`emoji`,`title`,`description`,`idx`) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (frappe.generate_hash("", 10), parent, "values", "Zeloura Settings", emoji, title, desc, order)
        )
    print(f"Values: {len(values)} entries inserted")

    # Insert Featured Items
    featured = [
        ("ZELOURA-001", "Cherry Ribbon Mini Dress", 4500, "New", 1),
        ("ZELOURA-006", "Lilac Bow Cardigan", 6800, "Hot", 2),
        ("ZELOURA-004", "Mint Cami Set", 9200, "Sale", 3),
        ("ZELOURA-007", "Butterfly Halter Top", 4200, "New", 4),
    ]
    for item, title, price, badge, order in featured:
        frappe.db.sql(
            "INSERT INTO `tabZeloura Product` (`name`,`parent`,`parentfield`,`parenttype`,`item`,`title`,`price`,`badge`,`idx`) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (frappe.generate_hash("", 10), parent, "featured_items", "Zeloura Settings", item, title, price, badge, order)
        )
    print(f"Featured: {len(featured)} entries inserted")

    # Insert Bestsellers
    bestsellers = [
        ("ZELOURA-002", "Velvet Slip Dress", 8900, 47, 1),
        ("ZELOURA-005", "Glaze Baby Tee", 2800, 128, 2),
        ("ZELOURA-003", "Daisy Cargo Pant", 5200, 64, 3),
        ("ZELOURA-008", "Cloud Trackshort Set", 3600, 83, 4),
        ("ZELOURA-001", "Cherry Ribbon Mini Dress", 4500, 92, 5),
    ]
    for item, title, price, sold, order in bestsellers:
        frappe.db.sql(
            "INSERT INTO `tabZeloura Bestseller` (`name`,`parent`,`parentfield`,`parenttype`,`item`,`title`,`price`,`sold_count`,`idx`) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (frappe.generate_hash("", 10), parent, "bestsellers", "Zeloura Settings", item, title, price, sold, order)
        )
    print(f"Bestsellers: {len(bestsellers)} entries inserted")

    # Insert Lookbook
    lookbook = [
        ("https://images.unsplash.com/photo-1515372039744-b8f02a3ae446?w=600&h=750&fit=crop&q=80", "Soft Girl Summer ???", "bows, ribbons, pastel dreams", 1),
        ("https://images.unsplash.com/photo-1434389677669-e08b4cac3105?w=600&h=750&fit=crop&q=80", "Clean Girl Mint ???", "iced lattes, linen sets", 2),
        ("https://images.unsplash.com/photo-1551163943-3f6a855d1153?w=600&h=750&fit=crop&q=80", "Y2K Dopamine ???", "yellow tights, sugar highs", 3),
        ("https://images.unsplash.com/photo-1571902943202-507ec2618e8f?w=600&h=750&fit=crop&q=80", "Night Drive ???", "indie sleaze, after-hours", 4),
        ("https://images.unsplash.com/photo-1596755094514-f87e34085b2c?w=600&h=750&fit=crop&q=80", "Lilac Hour ???", "coquette meets golden hour", 5),
    ]
    for img, title, subtitle, order in lookbook:
        frappe.db.sql(
            "INSERT INTO `tabZeloura Lookbook` (`name`,`parent`,`parentfield`,`parenttype`,`image`,`title`,`subtitle`,`idx`) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (frappe.generate_hash("", 10), parent, "lookbook_entries", "Zeloura Settings", img, title, subtitle, order)
        )
    print(f"Lookbook: {len(lookbook)} entries inserted")

    frappe.db.commit()

    # Verify
    print("\n=== Verification ===")
    scalar_count = frappe.db.sql("SELECT COUNT(*) FROM `tabSingles` WHERE `doctype`=%s", parent)[0][0]
    print(f"Scalar fields: {scalar_count} rows")
    for table, label in [("tabZeloura Value", "Values"), ("tabZeloura Product", "Featured"), ("tabZeloura Bestseller", "Bestsellers"), ("tabZeloura Lookbook", "Lookbook")]:
        count = frappe.db.sql(f"SELECT COUNT(*) FROM `{table}` WHERE `parent`=%s", parent)[0][0]
        print(f"{label}: {count} rows")

    print("\n=== All mock data populated successfully! ===")
    return "Done"
