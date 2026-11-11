"""Read-only replenishment quantities shared by the app and its local preview."""


def low_stock(stock, pending=None):
    pending = pending or {}
    result = []
    for source in stock:
        row = dict(source)
        minimum = float(row.get('minimum_qty') or 0)
        balance = float(row.get('balance') or 0)
        if balance >= 0 and (minimum <= 0 or balance > minimum):
            continue
        waiting = max(0, float(pending.get(row['item'], 0)))
        row.update(pending_qty=waiting,
                   suggested_qty=round(max(0, minimum - balance - waiting), 6))
        result.append(row)
    return sorted(result, key=lambda row: (row['balance'] / max(row['minimum_qty'],1), row['name']))
