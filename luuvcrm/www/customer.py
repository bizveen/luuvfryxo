"""/customer — the customer PWA's address until 2026-07-12. Redirects to /cus.

Kept because WhatsApp links carrying ?phone= were sent from this route while it
was live, and those threads do not expire.
"""

from luuvcrm.www.cus import redirect_to_cus

no_cache = 1


def get_context(context):
    redirect_to_cus()
