"""/pos2 — the counter's address until the three POS pages became one.

A redirect, kept for one release because the tablets are bookmarked to it.
Delete this file and pos2.html once the bookmarks are re-pointed.
"""

from luuvcrm.www.pos import redirect_to_pos

no_cache = 1


def get_context(context):
    redirect_to_pos()
