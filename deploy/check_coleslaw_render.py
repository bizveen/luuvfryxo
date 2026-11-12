"""Use the native live page renderer against this release."""
import sys
import coleslaw_preparation_release
sys.modules['recipe_sheets_release']=coleslaw_preparation_release
import check_recipe_sheets_render  # noqa: E402,F401
