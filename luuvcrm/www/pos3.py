"""/pos3 — the UI staging copy, until it became the counter page itself.

It carried the tablet layer, the iMin printer bridge and the Google-Docs print
layouts, so when the three POS pages collapsed it was pos3's file that became
`/pos`. The route was then deleted outright, which left every bookmark and
every history entry pointing at it answering **404 on a till**.

A redirect costs four lines and a 404 mid-service costs a queue at the counter.
Deleted with `pos2.*` once the tablets are re-pointed.
"""

from luuvcrm.www.pos import redirect_to_pos

no_cache = 1


def get_context(context):
    redirect_to_pos()
