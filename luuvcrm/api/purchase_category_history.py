"""Read-only category-change markers for purchase bill lists and reports."""
import json

import frappe


def _object(value):
    try:
        data = json.loads(value or '{}')
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def summary(doc, approval=None):
    """Use recorded changes, never a bill's modified time or mixed categories.

    Older approvals kept their original line categories in details_json. They
    can prove a change even before explicit category Versions were introduced.
    No caching: a correction response must immediately carry the new marker.
    """
    for row in frappe.get_all('Version',
            filters={'ref_doctype': 'Purchase Bill', 'docname': doc.name,
                     'data': ['like', '%purchase_category_%']},
            fields=['owner', 'creation', 'data'], order_by='creation desc, name desc'):
        data = _object(row.data)
        record = data.get('purchase_category_correction') or data.get('purchase_category_change') or {}
        changes = record.get('changes') or []
        if changes:
            return _result(changes, row.owner, row.creation, doc)

    if frappe.db.exists('DocType', 'LF Activity Log'):
        for row in frappe.get_all('LF Activity Log',
                filters={'ref_doctype': 'Purchase Bill', 'ref_name': doc.name,
                         'action': 'purchase_bill.edit'},
                fields=['user', 'creation', 'detail'], order_by='creation desc, name desc'):
            changes = _object(row.detail).get('line_categories') or []
            if changes:
                return _result(changes, row.user, row.creation, doc)

    original = _object(approval.get('details_json')).get('lines') if approval else None
    current = doc.get('items') or []
    # Do not guess row identity if the original receipt structure is different.
    if isinstance(original, list) and len(original) == len(current) and all(
            isinstance(old, dict) and old.get('name') == (new.item_name or new.item)
            for old, new in zip(original, current)):
        changes = [dict(row_name=new.name, before=old['category'], after=new.category)
                   for old, new in zip(original, current)
                   if old.get('category') and new.category and old['category'] != new.category]
        if changes:
            # A snapshot proves the change, but cannot prove who/when it happened.
            return _result(changes, '', None, doc)
    return {'category_changed': False, 'category_change': None}


def _result(changes, user, when, doc):
    names = {row.name: row.item_name or row.item or '' for row in doc.get('items') or []}
    normalized = [dict(name=change.get('name') or names.get(change.get('row_name'), ''),
                       before=change.get('before', change.get('from', '')),
                       after=change.get('after', change.get('to', '')))
                  for change in changes if isinstance(change, dict)
                  and change.get('before', change.get('from')) != change.get('after', change.get('to'))]
    return {'category_changed': bool(normalized), 'category_change': {
        'by': (frappe.get_value('User', user, 'full_name') or user) if user else '',
        'when': frappe.utils.format_datetime(when, 'd MMM yyyy, h:mm a') if when else '',
        'changes': normalized,
    } if normalized else None}
