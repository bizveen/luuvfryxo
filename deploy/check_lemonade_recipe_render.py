"""Native page and JavaScript checks for juice packaging drafts."""
import sys
import lemonade_recipe_release
sys.modules['recipe_sheets_release']=lemonade_recipe_release
import check_recipe_sheets_render  # noqa: E402,F401
