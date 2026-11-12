"""Native page and JavaScript checks for juice packaging drafts."""
import sys
import juice_packaging_release
sys.modules['recipe_sheets_release']=juice_packaging_release
import check_recipe_sheets_render  # noqa: E402,F401
