"""Add fixed-asset purchase categories and their ERPNext mappings."""

from luuvcrm.api.assets import ensure_asset_purchase_categories


def execute():
    ensure_asset_purchase_categories()
