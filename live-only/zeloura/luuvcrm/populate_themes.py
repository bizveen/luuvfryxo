import frappe

def populate():
    themes = [
        {"theme_name":"Calm","route":"calm","bg_color":"#FAF8F5","text_color":"#3D3A3A","muted_color":"#8A8686","accent_color":"#9CAF9A","accent_light":"#C5D5C3","surface_color":"#FFFFFF","border_color":"#E5E0DA","surface_alt":"#F5F0EB","display_font":"Playfair Display","body_font":"Inter","hero_layout":1},
        {"theme_name":"Dark","route":"dark","bg_color":"#0F0B14","text_color":"#E8E0F0","muted_color":"#8A7A9A","accent_color":"#C084FC","accent_light":"#3B2A50","surface_color":"#1A1525","border_color":"#2E2540","surface_alt":"#1A1525","display_font":"Space Grotesk","body_font":"Inter","hero_layout":2},
        {"theme_name":"Pastel","route":"pastel","bg_color":"#FFF5F9","text_color":"#5A4A52","muted_color":"#B8A0B0","accent_color":"#F0A8C0","accent_light":"#F8D0E0","surface_color":"#FFFFFF","border_color":"#F0D8E2","surface_alt":"#FFF0F5","display_font":"Caveat","body_font":"DM Sans","hero_layout":3},
        {"theme_name":"Bold","route":"bold","bg_color":"#FFF8E7","text_color":"#1A1A1A","muted_color":"#666666","accent_color":"#FF2D7B","accent_light":"#FFD6E5","surface_color":"#FFFFFF","border_color":"#FFB8CC","surface_alt":"#FFF0F5","display_font":"Bebas Neue","body_font":"DM Sans","hero_layout":4},
        {"theme_name":"Nature","route":"nature","bg_color":"#F7FAF5","text_color":"#2D3A2D","muted_color":"#6B7D6B","accent_color":"#5A8F5A","accent_light":"#C5DCC5","surface_color":"#FFFFFF","border_color":"#DCE8DC","surface_alt":"#EEF5EE","display_font":"Playfair Display","body_font":"Inter","hero_layout":5},
        {"theme_name":"Luxe","route":"luxe","bg_color":"#FCFAF5","text_color":"#1A1A1A","muted_color":"#8A8A8A","accent_color":"#C9A84C","accent_light":"#F5EDD0","surface_color":"#FFFFFF","border_color":"#E5DDC8","surface_alt":"#F8F5EE","display_font":"Playfair Display","body_font":"Inter","hero_layout":6},
        {"theme_name":"Retro","route":"retro","bg_color":"#FFF5E0","text_color":"#4A3020","muted_color":"#B08060","accent_color":"#E07030","accent_light":"#F5D0B0","surface_color":"#FFFDF5","border_color":"#E8CCB0","surface_alt":"#FFF0E0","display_font":"Bebas Neue","body_font":"DM Sans","hero_layout":7},
        {"theme_name":"Minimal","route":"minimal","bg_color":"#FFFFFF","text_color":"#000000","muted_color":"#999999","accent_color":"#000000","accent_light":"#EEEEEE","surface_color":"#FFFFFF","border_color":"#DDDDDD","surface_alt":"#F5F5F5","display_font":"Inter","body_font":"Inter","hero_layout":8},
        {"theme_name":"Ocean","route":"ocean","bg_color":"#F0F8FF","text_color":"#1A3A4A","muted_color":"#5A8A9A","accent_color":"#2A9DB8","accent_light":"#B0E0EE","surface_color":"#FFFFFF","border_color":"#C8E0EC","surface_alt":"#E8F4FA","display_font":"Playfair Display","body_font":"Inter","hero_layout":9},
        {"theme_name":"Berry","route":"berry","bg_color":"#1A0A1A","text_color":"#F5E0F0","muted_color":"#A07090","accent_color":"#D04080","accent_light":"#4A203A","surface_color":"#2A1525","border_color":"#3A2030","surface_alt":"#2A1525","display_font":"Unbounded","body_font":"DM Sans","hero_layout":10},
    ]
    
    for t in themes:
        if not frappe.db.exists("Zeloura Theme", {"route": t["route"]}):
            doc = frappe.get_doc({"doctype": "Zeloura Theme"})
            doc.update(t)
            doc.insert()
    
    frappe.db.commit()
    return f"Created {len(themes)} Zeloura Theme records!"
