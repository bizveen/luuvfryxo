"""Per-document ERPNext allowance for an already checked branch POS issue.

Used only by our server-side submission helper; no global Item/Stock Settings
flags and no bench-wide controller replacement. All other validations run.
"""
from erpnext.stock.doctype.stock_entry.stock_entry import StockEntry
from erpnext.stock.doctype.stock_reconciliation.stock_reconciliation import StockReconciliation
from erpnext.stock.stock_ledger import get_previous_sle


class BranchStockEntry(StockEntry):
    def set_actual_qty(self):
        if not self.flags.luuv_checked_negative_stock:
            return super().set_actual_qty()
        for row in self.get('items'):
            previous=get_previous_sle(dict(item_code=row.item_code,
                warehouse=row.s_warehouse or row.t_warehouse,
                posting_date=self.posting_date,posting_time=self.posting_time))
            row.actual_qty=previous.get('qty_after_transaction') or 0

    def update_stock_ledger(self, allow_negative_stock=False):
        return super().update_stock_ledger(allow_negative_stock=bool(
            allow_negative_stock or self.flags.luuv_checked_negative_stock))


class BranchStockReconciliation(StockReconciliation):
    """Reconcile an existing deficit to a validated nonnegative physical count."""
    def update_stock_ledger(self, allow_negative_stock=False):
        return super().update_stock_ledger(allow_negative_stock=bool(
            allow_negative_stock or self.flags.luuv_correcting_negative_stock))
