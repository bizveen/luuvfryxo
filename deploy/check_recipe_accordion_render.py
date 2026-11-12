"""Run native rendering/syntax checks against the accordion release."""
import sys
import recipe_accordion_release

sys.modules['recipe_sheets_release'] = recipe_accordion_release
import check_recipe_sheets_render  # noqa: E402,F401
