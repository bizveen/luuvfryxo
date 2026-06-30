import frappe

def create_doctype(doctype_name, module, fields, is_child=0, is_single=0):
    if frappe.db.exists("DocType", doctype_name):
        return doctype_name + " already exists"
    doc = frappe.get_doc({
        "doctype": "DocType",
        "name": doctype_name,
        "module": module,
        "custom": 1,
        "is_submittable": 0,
        "is_child_table": is_child,
        "istable": is_child,
        "is_single": is_single,
        "in_create": 0 if is_child else 1,
        "fields": fields,
        "permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}]
    })
    doc.insert()
    return "created " + doctype_name

def run():
    frappe.db.sql("SET foreign_key_checks = 0")
    
    print(create_doctype("Zeloura Product", "Zeloura", [
        {"fieldname": "item", "label": "Item", "fieldtype": "Link", "options": "Item", "in_list_view": 1},
        {"fieldname": "title", "label": "Override Title", "fieldtype": "Data", "in_list_view": 1},
        {"fieldname": "price", "label": "Override Price", "fieldtype": "Currency"},
        {"fieldname": "image", "label": "Override Image", "fieldtype": "Attach Image"},
        {"fieldname": "badge", "label": "Badge", "fieldtype": "Select", "options": "\nNew\nSale\nHot", "in_list_view": 1},
        {"fieldname": "state_label", "label": "State Label", "fieldtype": "Data"},
        {"fieldname": "order", "label": "Order", "fieldtype": "Int", "in_list_view": 1},
    ], is_child=1))

    print(create_doctype("Zeloura Value", "Zeloura", [
        {"fieldname": "emoji", "label": "Icon/Emoji", "fieldtype": "Data", "in_list_view": 1},
        {"fieldname": "title", "label": "Title", "fieldtype": "Data", "in_list_view": 1},
        {"fieldname": "description", "label": "Description", "fieldtype": "Small Text"},
        {"fieldname": "order", "label": "Order", "fieldtype": "Int", "in_list_view": 1},
    ], is_child=1))

    print(create_doctype("Zeloura Lookbook", "Zeloura", [
        {"fieldname": "image", "label": "Image", "fieldtype": "Attach Image"},
        {"fieldname": "title", "label": "Title", "fieldtype": "Data", "in_list_view": 1},
        {"fieldname": "subtitle", "label": "Subtitle", "fieldtype": "Data"},
        {"fieldname": "link", "label": "Link", "fieldtype": "Data"},
        {"fieldname": "order", "label": "Order", "fieldtype": "Int", "in_list_view": 1},
    ], is_child=1))

    print(create_doctype("Zeloura Bestseller", "Zeloura", [
        {"fieldname": "item", "label": "Item", "fieldtype": "Link", "options": "Item", "in_list_view": 1},
        {"fieldname": "title", "label": "Override Title", "fieldtype": "Data", "in_list_view": 1},
        {"fieldname": "price", "label": "Override Price", "fieldtype": "Currency"},
        {"fieldname": "image", "label": "Override Image", "fieldtype": "Attach Image"},
        {"fieldname": "sold_count", "label": "Sold Today", "fieldtype": "Int", "in_list_view": 1},
        {"fieldname": "order", "label": "Order", "fieldtype": "Int", "in_list_view": 1},
    ], is_child=1))

    print(create_doctype("Zeloura Settings", "Zeloura", [
        {"fieldname": "sb_hero", "label": "Hero Section", "fieldtype": "Section Break"},
        {"fieldname": "cb_hero", "label": "", "fieldtype": "Column Break"},
        {"fieldname": "hero_title", "label": "Hero Title", "fieldtype": "Data", "default": "main character era"},
        {"fieldname": "hero_subtitle", "label": "Hero Subtitle", "fieldtype": "Small Text", "default": "the drop you have been waiting for."},
        {"fieldname": "hero_badge", "label": "Hero Badge", "fieldtype": "Data", "default": "new drop just dropped"},
        {"fieldname": "cb_hero2", "label": "", "fieldtype": "Column Break"},
        {"fieldname": "hero_cta_text", "label": "CTA Button Text", "fieldtype": "Data", "default": "shop the drop"},
        {"fieldname": "hero_cta_link", "label": "CTA Button Link", "fieldtype": "Data", "default": "/shop"},
        {"fieldname": "hero_secondary_text", "label": "Secondary Button", "fieldtype": "Data", "default": "explore"},
        {"fieldname": "hero_secondary_link", "label": "Secondary Link", "fieldtype": "Data", "default": "#"},
        {"fieldname": "hero_product_image", "label": "Product Image", "fieldtype": "Attach Image"},
        {"fieldname": "hero_product_name", "label": "Product Name", "fieldtype": "Data", "default": "the coquette set"},
        {"fieldname": "hero_product_price", "label": "Product Price", "fieldtype": "Currency", "default": 89},
        {"fieldname": "sb_featured", "label": "Featured Products", "fieldtype": "Section Break"},
        {"fieldname": "featured_title", "label": "Section Title", "fieldtype": "Data", "default": "find your fit"},
        {"fieldname": "featured_subtitle", "label": "Section Subtitle", "fieldtype": "Data", "default": "who are you this week"},
        {"fieldname": "featured_items", "label": "Featured Items", "fieldtype": "Table", "options": "Zeloura Product"},
        {"fieldname": "sb_bestsellers", "label": "Bestsellers", "fieldtype": "Section Break"},
        {"fieldname": "bestsellers_title", "label": "Section Title", "fieldtype": "Data", "default": "trending now"},
        {"fieldname": "bestsellers_subtitle", "label": "Section Subtitle", "fieldtype": "Data", "default": "bestsellers ss26"},
        {"fieldname": "bestsellers", "label": "Bestseller Items", "fieldtype": "Table", "options": "Zeloura Bestseller"},
        {"fieldname": "sb_values", "label": "Brand Values", "fieldtype": "Section Break"},
        {"fieldname": "values_title", "label": "Section Title", "fieldtype": "Data", "default": "what we stand for"},
        {"fieldname": "values_subtitle", "label": "Section Subtitle", "fieldtype": "Data", "default": "values we dress in"},
        {"fieldname": "values", "label": "Values", "fieldtype": "Table", "options": "Zeloura Value"},
        {"fieldname": "sb_lookbook", "label": "Lookbook", "fieldtype": "Section Break"},
        {"fieldname": "lookbook_title", "label": "Section Title", "fieldtype": "Data", "default": "editorial"},
        {"fieldname": "lookbook_subtitle", "label": "Section Subtitle", "fieldtype": "Data", "default": "the lookbook ss26"},
        {"fieldname": "lookbook_entries", "label": "Entries", "fieldtype": "Table", "options": "Zeloura Lookbook"},
        {"fieldname": "sb_news", "label": "Newsletter", "fieldtype": "Section Break"},
        {"fieldname": "newsletter_title", "label": "Title", "fieldtype": "Data", "default": "join the club"},
        {"fieldname": "newsletter_subtitle", "label": "Subtitle", "fieldtype": "Data", "default": "pink club"},
        {"fieldname": "newsletter_description", "label": "Description", "fieldtype": "Small Text", "default": "get early access"},
        {"fieldname": "newsletter_cta", "label": "CTA Text", "fieldtype": "Data", "default": "join free"},
        {"fieldname": "sb_about", "label": "About / Story", "fieldtype": "Section Break"},
        {"fieldname": "about_subtitle", "label": "Section Subtitle", "fieldtype": "Data", "default": "our story"},
        {"fieldname": "about_title", "label": "Title", "fieldtype": "Data", "default": "made for girls who run the day"},
        {"fieldname": "about_content", "label": "Content", "fieldtype": "Text Editor", "default": "zeloura collective isn't just clothing..."},
        {"fieldname": "about_image", "label": "Image", "fieldtype": "Attach Image"},
        {"fieldname": "about_stat1_label", "label": "Stat 1 Label", "fieldtype": "Data", "default": "happy besties"},
        {"fieldname": "about_stat1_value", "label": "Stat 1 Value", "fieldtype": "Data", "default": "15k+"},
    ], is_single=1))

    frappe.db.commit()
    frappe.db.sql("SET foreign_key_checks = 1")
    return "All done!"

if __name__ == "__main__":
    print(run())
