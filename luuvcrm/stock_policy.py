"""Branch POS shortage rules. Percentages cap the resulting negative balance.

The denominator is the saved recipe usage for the entire order being sent,
in each ingredient's stock unit. Existing deficits count toward the limit.
"""
import math

MODES = ('block', 'percent', 'negative')


def normalize(value=None):
    value = value or {}
    if not isinstance(value, dict): raise ValueError('Invalid stock rules.')
    def rule(raw):
        if not isinstance(raw, dict) or raw.get('mode', 'block') not in MODES:
            raise ValueError('Choose Block, Percentage tolerance or Allow negative.')
        mode = raw.get('mode', 'block')
        try: percent = float(raw.get('percent', 0))
        except (TypeError, ValueError): percent = float('nan')
        if not math.isfinite(percent) or not 0 <= percent <= 100:
            raise ValueError('Tolerance must be between 0 and 100%.')
        return dict(mode=mode, percent=percent if mode=='percent' else 0)
    overrides = value.get('items') or {}
    if not isinstance(overrides, dict) or len(overrides)>2000:
        raise ValueError('Invalid ingredient rules.')
    try: revision = int(value.get('revision', 0))
    except (ValueError, TypeError): raise ValueError('Reload the stock rules.')
    if revision < 0: raise ValueError('Reload the stock rules.')
    return dict(revision=revision, default=rule(value.get('default') or {}),
                items={str(item):rule(raw) for item,raw in overrides.items()})


def assess(policy, deductions):
    policy = normalize(policy)
    warnings, blocked = [], []
    for row in deductions:
        before, used = float(row['before']), float(row['qty'])
        if not math.isfinite(before) or not math.isfinite(used) or used <= 0:
            raise ValueError('Invalid stock consumption quantity.')
        after = round(before-used, 6)
        if after >= -0.0000001: continue
        rule = policy['items'].get(row['item'], policy['default'])
        limit = round(used*rule['percent']/100, 6) if rule['mode']=='percent' else 0
        allowed = rule['mode']=='negative' or (rule['mode']=='percent' and -after<=limit+0.0000001)
        notice = dict(row, after=after, mode=rule['mode'], percent=rule['percent'],
                      limit=None if rule['mode']=='negative' else limit)
        (warnings if allowed else blocked).append(notice)
    return dict(warnings=warnings, blocked=blocked)


def rejection(rows):
    return 'Insufficient stock: '+ '; '.join(
        f"{r['name']} would be {r['after']:g} {r['uom']}" +
        (f" (allowed minus {r['limit']:g} {r['uom']}, {r['percent']:g}% of this order's recipe usage)"
         if r['mode']=='percent' else ' (negative stock blocked)') for r in rows)
