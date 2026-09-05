"""/my_orders — the same page as /myorders, under the underscored spelling.

Both were live, so both stay as aliases to /cus. Which one a given customer has
depends on which message they were sent.
"""

from luuvcrm.www.cus import redirect_to_cus

no_cache = 1


def get_context(context):
    redirect_to_cus()
