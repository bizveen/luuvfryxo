import frappe

def migrate():
    # Get old settings data
    old = frappe.get_single("Zeloura Settings")
    scalar_fields = [
        "hero_title","hero_subtitle","hero_badge","hero_cta_text","hero_image",
        "featured_title","featured_subtitle",
        "bestsellers_title","bestsellers_subtitle",
        "values_title","values_subtitle",
        "lookbook_title","lookbook_subtitle",
        "about_subtitle","about_title","about_content",
        "about_stat1_label","about_stat1_value",
        "about_stat2_label","about_stat2_value",
        "newsletter_title","newsletter_subtitle","newsletter_description",
    ]
    scalar_data = {}
    for f in scalar_fields:
        val = old.get(f)
        if val:
            scalar_data[f] = val

    # Get child table data from old settings
    def get_child_data(table_field):
        data = []
        for row in old.get(table_field) or []:
            row_data = {}
            for rf in row.meta.fields:
                rv = row.get(rf.fieldname)
                if rv:
                    row_data[rf.fieldname] = rv
            data.append(row_data)
        return data

    featured_data = get_child_data("featured_items")
    bestsellers_data = get_child_data("bestsellers")
    values_data = get_child_data("values")
    lookbook_data = get_child_data("lookbook_entries")

    # Copy to each theme
    themes = frappe.get_all("Zeloura Theme", fields=["name", "route", "theme_name"])
    count = 0
    for t in themes:
        doc = frappe.get_doc("Zeloura Theme", t.name)
        # Set scalar fields
        for key, val in scalar_data.items():
            doc.set(key, val)
        # Clear old child table data
        doc.set("featured_items", [])
        doc.set("bestsellers", [])
        doc.set("values", [])
        doc.set("lookbook_entries", [])
        # Set child table data
        for item_data in featured_data:
            child = doc.append("featured_items", {})
            for k, v in item_data.items():
                child.set(k, v)
        for item_data in bestsellers_data:
            child = doc.append("bestsellers", {})
            for k, v in item_data.items():
                child.set(k, v)
        for item_data in values_data:
            child = doc.append("values", {})
            for k, v in item_data.items():
                child.set(k, v)
        for item_data in lookbook_data:
            child = doc.append("lookbook_entries", {})
            for k, v in item_data.items():
                child.set(k, v)
        doc.save(ignore_permissions=True)
        count += 1

    frappe.db.commit()
    return f"Migrated data to {count} themes"
