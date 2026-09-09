import frappe

def populate():
    # Set scalar fields
    fields = {
        "hero_title": "Zeloura",
        "hero_subtitle": "main character era",
        "hero_badge": "New Collection 2026",
        "hero_cta_text": "shop the drop",
        "hero_cta_link": "/shop",
        "hero_image": "https://images.unsplash.com/photo-1515372039744-b8f02a3ae446?w=800&h=1000&fit=crop&q=80",
        "featured_title": "Shop the Collection",
        "featured_subtitle": "Featured",
        "bestsellers_title": "Trending Now",
        "bestsellers_subtitle": "Bestsellers",
        "values_title": "Our Values",
        "values_subtitle": "Why Us",
        "lookbook_title": "Get the Look",
        "lookbook_subtitle": "Lookbook",
        "about_subtitle": "Our Story",
        "about_title": "About Zeloura",
        "about_content": "Zeloura is where timeless design meets modern sensibility. Every piece is crafted with care, designed to make you feel confident and beautiful.",
        "about_stat1_label": "Products",
        "about_stat1_value": "500+",
        "about_stat2_label": "Customers",
        "about_stat2_value": "10K+",
        "newsletter_title": "Join the Club",
        "newsletter_subtitle": "Stay Connected",
        "newsletter_description": "Be the first to know about new drops, exclusive offers, and style inspiration straight to your inbox.",
    }
    for key, val in fields.items():
        frappe.db.set_single_value("Zeloura Settings", key, val)
    
    # Clear and repopulate featured_items child table
    frappe.db.delete("Zeloura Product", {"parent": "Zeloura Settings"})
    featured = [
        {"item": "ZELOURA-001", "title": "Cherry Ribbon Mini Dress", "price": 4500, "badge": "New", "image": "https://images.unsplash.com/photo-1515372039744-b8f02a3ae446?w=400&h=600&fit=crop&q=80"},
        {"item": "ZELOURA-002", "title": "Velvet Slip Dress", "price": 8900, "badge": "Hot", "image": "https://images.unsplash.com/photo-1434389677669-e08b4cac3105?w=400&h=600&fit=crop&q=80"},
        {"item": "ZELOURA-003", "title": "Daisy Cargo Pant", "price": 5200, "badge": "Sale", "image": "https://images.unsplash.com/photo-1551163943-3f6a855d1153?w=400&h=600&fit=crop&q=80"},
        {"item": "ZELOURA-004", "title": "Mint Cami Set", "price": 9200, "badge": "New", "image": "https://images.unsplash.com/photo-1556909114-f6e7ad7d3136?w=400&h=600&fit=crop&q=80"},
    ]
    for item in featured:
        doc = frappe.new_doc("Zeloura Product")
        doc.parent = "Zeloura Settings"
        doc.parentfield = "featured_items"
        doc.parenttype = "Zeloura Settings"
        doc.item = item["item"]
        doc.title = item["title"]
        doc.price = item["price"]
        doc.badge = item["badge"]
        doc.image = item["image"]
        doc.insert()

    # Clear and repopulate bestsellers child table
    frappe.db.delete("Zeloura Bestseller", {"parent": "Zeloura Settings"})
    bestsellers = [
        {"item": "ZELOURA-005", "title": "Glaze Baby Tee", "price": 2800, "image": "https://images.unsplash.com/photo-1576566588028-4147f3842f27?w=400&h=600&fit=crop&q=80"},
        {"item": "ZELOURA-006", "title": "Lilac Bow Cardigan", "price": 6800, "image": "https://images.unsplash.com/photo-1556909114-f6e7ad7d3136?w=400&h=600&fit=crop&q=80"},
        {"item": "ZELOURA-007", "title": "Butterfly Halter Top", "price": 4200, "image": "https://images.unsplash.com/photo-1564257631407-4deb1f99d992?w=400&h=600&fit=crop&q=80"},
        {"item": "ZELOURA-008", "title": "Cloud Trackshort Set", "price": 3600, "image": "https://images.unsplash.com/photo-1591195853828-11db59a44f6b?w=400&h=600&fit=crop&q=80"},
    ]
    for item in bestsellers:
        doc = frappe.new_doc("Zeloura Bestseller")
        doc.parent = "Zeloura Settings"
        doc.parentfield = "bestsellers"
        doc.parenttype = "Zeloura Settings"
        doc.item = item["item"]
        doc.title = item["title"]
        doc.price = item["price"]
        doc.image = item["image"]
        doc.insert()
    
    # Clear and repopulate values
    frappe.db.delete("Zeloura Value", {"parent": "Zeloura Settings"})
    values = [
        {"emoji": "✨", "title": "Quality First", "description": "Premium materials and expert craftsmanship in every piece."},
        {"emoji": "❤️", "title": "Ethical Production", "description": "Fair wages and safe conditions for all our makers."},
        {"emoji": "♻️", "title": "Sustainable", "description": "Eco-friendly fabrics and packaging for a better planet."},
    ]
    for v in values:
        doc = frappe.new_doc("Zeloura Value")
        doc.parent = "Zeloura Settings"
        doc.parentfield = "values"
        doc.parenttype = "Zeloura Settings"
        doc.emoji = v["emoji"]
        doc.title = v["title"]
        doc.description = v["description"]
        doc.insert()
    
    # Clear and repopulate lookbook
    frappe.db.delete("Zeloura Lookbook", {"parent": "Zeloura Settings"})
    lookbook = [
        {"image": "https://images.unsplash.com/photo-1515372039744-b8f02a3ae446?w=600&h=750&fit=crop&q=80", "title": "Evening Glam", "subtitle": "Dressed to impress"},
        {"image": "https://images.unsplash.com/photo-1434389677669-e08b4cac3105?w=600&h=750&fit=crop&q=80", "title": "Casual Chic", "subtitle": "Everyday elegance"},
        {"image": "https://images.unsplash.com/photo-1551163943-3f6a855d1153?w=600&h=750&fit=crop&q=80", "title": "Street Style", "subtitle": "Urban edge"},
    ]
    for lb in lookbook:
        doc = frappe.new_doc("Zeloura Lookbook")
        doc.parent = "Zeloura Settings"
        doc.parentfield = "lookbook_entries"
        doc.parenttype = "Zeloura Settings"
        doc.image = lb["image"]
        doc.title = lb["title"]
        doc.subtitle = lb["subtitle"]
        doc.insert()
    
    frappe.db.commit()
    print("Zeloura Settings populated successfully!")
