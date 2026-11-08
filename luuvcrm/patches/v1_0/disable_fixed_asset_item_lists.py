"""Make fixed-asset purchase categories free-text instead of item pickers."""

from luuvcrm.api.assets import ensure_asset_purchase_categories


def execute():
    ensure_asset_purchase_categories()
