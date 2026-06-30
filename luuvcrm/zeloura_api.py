import frappe, json

@frappe.whitelist(allow_guest=True)
def get_page_data():
    return _fetch_theme_data(frappe.form_dict.get("route") or "")

def _fetch_theme_data(route):
    theme_name = None
    if route:
        themes = frappe.get_all("Zeloura Theme", filters={"route": route}, fields=["name"], limit=1)
        if themes:
            theme_name = themes[0].name
    
    if not theme_name:
        fallback = frappe.get_all("Zeloura Theme", fields=["name"], limit=1, order_by="name asc")
        if fallback:
            theme_name = fallback[0].name
    
    out = {"settings": {}}
    
    if theme_name:
        theme = frappe.get_doc("Zeloura Theme", theme_name)
        for field in theme.meta.fields:
            if field.fieldtype == "Table":
                if field.fieldname in ("featured_items", "bestsellers"):
                    continue
                child_data = []
                for row in theme.get(field.fieldname) or []:
                    row_data = {f.fieldname: row.get(f.fieldname) for f in row.meta.fields}
                    child_data.append(row_data)
                out["settings"][field.fieldname] = child_data
            elif field.fieldtype == "Currency":
                out["settings"][field.fieldname] = float(theme.get(field.fieldname) or 0)
            elif field.fieldtype in ("Data", "Small Text", "Text Editor", "Select", "Int", "Color"):
                out["settings"][field.fieldname] = theme.get(field.fieldname)
            elif field.fieldtype == "Attach Image":
                out["settings"][field.fieldname] = theme.get(field.fieldname)
            elif field.fieldtype == "Check":
                out["settings"][field.fieldname] = bool(theme.get(field.fieldname))
            elif field.fieldtype == "Long Text":
                out["settings"][field.fieldname] = theme.get(field.fieldname)
            else:
                out["settings"][field.fieldname] = theme.get(field.fieldname)
    
    try:
        settings = frappe.get_single("Zeloura Settings")
        theme_route = route or (frappe.get_all("Zeloura Theme", fields=["route"], limit=1, order_by="name asc") or [{}])[0].get("route", "calm")
        
        featured = []
        for row in settings.featured_items or []:
            themes_val = (row.themes or "").strip()
            if themes_val and theme_route not in [t.strip() for t in themes_val.split(",")]:
                continue
            featured.append(_resolve_item(row))
        out["settings"]["featured_items"] = featured
        
        bestsellers = []
        for row in settings.bestsellers or []:
            themes_val = (row.themes or "").strip()
            if themes_val and theme_route not in [t.strip() for t in themes_val.split(",")]:
                continue
            bestsellers.append(_resolve_item(row))
        out["settings"]["bestsellers"] = bestsellers
    except Exception as e:
        if not out["settings"].get("featured_items"):
            out["settings"]["featured_items"] = []
        if not out["settings"].get("bestsellers"):
            out["settings"]["bestsellers"] = []
    
    return out

def _resolve_item(row):
    raw_item_code = row.item or ""
    item_code = raw_item_code.strip()
    title = (row.title or "").strip() or None
    price = row.price or None
    image = row.image or None
    badge = getattr(row, "badge", None) or None
    sold_count = getattr(row, "sold_count", None) or None
    sizes = None
    images_list = None
    colors_list = None
    
    if item_code and frappe.db.exists("Item", item_code):
        item_data = frappe.db.get_value("Item", item_code, ["item_name", "image", "z_sizes", "z_images", "z_colors"], as_dict=True)
        if item_data:
            if not title:
                title = item_data.get("item_name") or item_code
            if not image:
                image = item_data.get("image")
            raw_sizes = item_data.get("z_sizes")
            if raw_sizes:
                try:
                    parsed = json.loads(raw_sizes)
                    if isinstance(parsed, list):
                        sizes = parsed
                except (json.JSONDecodeError, TypeError):
                    pass
            raw_images = item_data.get("z_images")
            if raw_images:
                try:
                    parsed = json.loads(raw_images)
                    if isinstance(parsed, list):
                        images_list = parsed
                except (json.JSONDecodeError, TypeError):
                    pass
            raw_colors = item_data.get("z_colors")
            if raw_colors:
                try:
                    parsed = json.loads(raw_colors)
                    if isinstance(parsed, list):
                        colors_list = parsed
                except (json.JSONDecodeError, TypeError):
                    pass
        
        if not price:
            price_data = frappe.db.get_value("Item Price",
                {"item_code": item_code}, "price_list_rate")
            if price_data:
                price = float(price_data)
    
    result = {
        "item": item_code,
        "title": title or item_code or "Product",
        "image": image or "",
        "price": price or 0,
        "badge": badge or "",
        "sold_count": sold_count or 0,
    }
    if sizes:
        result["sizes"] = sizes
    if images_list:
        result["images"] = images_list
    if colors_list:
        result["colors"] = colors_list
    
    return result

@frappe.whitelist(allow_guest=True)
def get_themes():
    themes = frappe.get_all("Zeloura Theme", fields=["*"], order_by="name asc")
    return {"themes": themes}

@frappe.whitelist(allow_guest=True)
def get_landing():
    """Returns all themes with 3 preview product images each."""
    themes = frappe.get_all("Zeloura Theme", fields=["*"], order_by="name asc")
    try:
        settings = frappe.get_single("Zeloura Settings")
    except Exception:
        return {"themes": themes}
    
    for t in themes:
        route = t.get("route", "")
        preview_items = []
        for row in list(settings.featured_items or []) + list(settings.bestsellers or []):
            themes_val = (row.themes or "").strip()
            if not themes_val or route not in [r.strip() for r in themes_val.split(",")]:
                continue
            resolved = _resolve_item(row)
            if resolved.get("image"):
                preview_items.append(resolved)
            if len(preview_items) >= 3:
                break
        t["preview_items"] = preview_items
    
    return {"themes": themes}

@frappe.whitelist(allow_guest=True)
def get_csrf():
    from frappe.sessions import get_csrf_token
    return {"csrf_token": get_csrf_token()}
