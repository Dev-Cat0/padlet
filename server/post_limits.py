"""Plan limits for an explicitly chosen per-section post count; zero stays unlimited."""

STANDARD_POST_LIMIT = 5000
PERMANENT_POST_LIMIT = 1000000


def post_limit(access):
    permanent = ("remaining" in access and "expires" in access
                 and access["remaining"] is None and access["expires"] is None)
    return PERMANENT_POST_LIMIT if permanent else STANDARD_POST_LIMIT
