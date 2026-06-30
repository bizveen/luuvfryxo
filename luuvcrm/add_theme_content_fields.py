import frappe

def run():
    """Add content fields and tables to Zeloura Theme"""
    doctype = "Zeloura Theme"
    if not frappe.db.exists("DocType", doctype):
        return "Zeloura Theme not found"
    
    doc = frappe.get_doc("DocType", doctype)
    existing = {f.fieldname for f in doc.fields}
    
    new_fields = [
        # Hero section
        {"fieldname": "sb_content", "label": "Content Settings", "fieldtype": "Section Break", "insert_after": "hero_layout"},
        {"fieldname": "hero_title", "label": "Hero Title", "fieldtype": "Data", "default": ""},
        {"fieldname": "hero_subtitle", "label": "Hero Subtitle", "fieldtype": "Data", "default": ""},
        {"fieldname": "hero_badge", "label": "Hero Badge", "fieldtype": "Data", "default": ""},
        {"fieldname": "hero_cta_text", "label": "Hero CTA Text", "fieldtype": "Data", "default": ""},
        {"fieldname": "hero_image", "label": "Hero Image", "fieldtype": "Attach Image"},
        # Featured
        {"fieldname": "sb_featured", "label": "Featured Section", "fieldtype": "Section Break"},
        {"fieldname": "featured_title", "label": "Featured Title", "fieldtype": "Data", "default": "Shop the Collection"},
        {"fieldname": "featured_subtitle", "label": "Featured Subtitle", "fieldtype": "Data", "default": "Featured"},
        {"fieldname": "featured_items", "label": "Featured Items", "fieldtype": "Table", "options": "Zeloura Product"},
        # Bestsellers
        {"fieldname": "sb_bestsellers", "label": "Bestsellers Section", "fieldtype": "Section Break"},
        {"fieldname": "bestsellers_title", "label": "Bestsellers Title", "fieldtype": "Data", "default": "Trending Now"},
        {"fieldname": "bestsellers_subtitle", "label": "Bestsellers Subtitle", "fieldtype": "Data", "default": "Bestsellers"},
        {"fieldname": "bestsellers", "label": "Bestsellers", "fieldtype": "Table", "options": "Zeloura Bestseller"},
        # Values
        {"fieldname": "sb_values", "label": "Values Section", "fieldtype": "Section Break"},
        {"fieldname": "values_title", "label": "Values Title", "fieldtype": "Data", "default": "Our Values"},
        {"fieldname": "values_subtitle", "label": "Values Subtitle", "fieldtype": "Data", "default": "Why Us"},
        {"fieldname": "values", "label": "Values", "fieldtype": "Table", "options": "Zeloura Value"},
        # Lookbook
        {"fieldname": "sb_lookbook", "label": "Lookbook Section", "fieldtype": "Section Break"},
        {"fieldname": "lookbook_title", "label": "Lookbook Title", "fieldtype": "Data", "default": "Get the Look"},
        {"fieldname": "lookbook_subtitle", "label": "Lookbook Subtitle", "fieldtype": "Data", "default": "Lookbook"},
        {"fieldname": "lookbook_entries", "label": "Lookbook Entries", "fieldtype": "Table", "options": "Zeloura Lookbook"},
        # About
        {"fieldname": "sb_about", "label": "About Section", "fieldtype": "Section Break"},
        {"fieldname": "about_subtitle", "label": "About Subtitle", "fieldtype": "Data", "default": "Our Story"},
        {"fieldname": "about_title", "label": "About Title", "fieldtype": "Data", "default": "About"},
        {"fieldname": "about_content", "label": "About Content", "fieldtype": "Text Editor", "default": ""},
        {"fieldname": "about_stat1_label", "label": "Stat 1 Label", "fieldtype": "Data", "default": "Products"},
        {"fieldname": "about_stat1_value", "label": "Stat 1 Value", "fieldtype": "Data", "default": "500+"},
        {"fieldname": "about_stat2_label", "label": "Stat 2 Label", "fieldtype": "Data", "default": "Customers"},
        {"fieldname": "about_stat2_value", "label": "Stat 2 Value", "fieldtype": "Data", "default": "10K+"},
        # Newsletter
        {"fieldname": "sb_newsletter", "label": "Newsletter Section", "fieldtype": "Section Break"},
        {"fieldname": "newsletter_title", "label": "Newsletter Title", "fieldtype": "Data", "default": "Join Us"},
        {"fieldname": "newsletter_subtitle", "label": "Newsletter Subtitle", "fieldtype": "Data", "default": "Stay Connected"},
        {"fieldname": "newsletter_description", "label": "Newsletter Description", "fieldtype": "Small Text", "default": ""},
    ]
    
    added = 0
    for f in new_fields:
        fn = f["fieldname"]
        if fn not in existing:
            # Find insert_after position
            ins = f.get("insert_after", "hero_layout")
            pos = -1
            for i, ef in enumerate(doc.fields):
                if ef.fieldname == ins:
                    pos = i + 1
                    break
            # Remove insert_after from dict (not a field property)
            f_copy = {k:v for k,v in f.items() if k != "insert_after"}
            df = doc.append("fields", f_copy)
            if pos >= 0:
                df.idx = pos + 1
            added += 1
    
    if added:
        doc.save(ignore_permissions=True)
        frappe.db.commit()
    
    return f"Added {added} new fields to Zeloura Theme"
