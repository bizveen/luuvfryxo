"""Native page and JavaScript checks for the active-recipe view."""
import sys
import recipe_activation_release
sys.modules['recipe_sheets_release']=recipe_activation_release
import check_recipe_sheets_render  # noqa: E402,F401
