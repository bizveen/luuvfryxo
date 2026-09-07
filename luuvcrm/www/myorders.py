"""/myorders — the old order-history page. Redirects to /cus, which absorbed it.

Kept as an alias: the WhatsApp auto-reply pointed customers here for months, so
the link is sitting in their chat history.
"""

from luuvcrm.www.cus import redirect_to_cus

no_cache = 1


def get_context(context):
    redirect_to_cus()
