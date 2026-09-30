# INFO: Rewrites the size suffix of a provider CDN image URL to a square.

import re

# The provider's CDN reads the image size from a suffix such as
# =w544-h544-p-l90-rj (width, height, -p = smart crop). [0-9] and not \d,
# because \d also accepts non-ASCII digits. -p counts only as a whole flag,
# followed by - or by the end of the URL, so -pd is another flag and is never
# taken for -p.
_SIZE_SUFFIX = r"=w[0-9]+-h[0-9]+"
_SIZE_SUFFIX_WITH_SMART_CROP = _SIZE_SUFFIX + r"(?:-p(?=-|$))?"


def square_thumbnail_url(url: str | None, size: int, *, smart_crop: bool) -> str | None:
    # Replaces only the first occurrence, with no host filter. A URL without
    # the size suffix is returned as is, never as an error; None gives None.
    # smart_crop=True leaves exactly one -p after the size (adds it when
    # missing, does not duplicate it) and is idempotent when the suffix is
    # followed by - or by the end of the URL, which is the provider's format.
    # smart_crop=False does not touch what follows, -p included: that branch
    # is the rule of migration 036 (see _normalize_thumbnail_url in
    # services/activity_service.py) and the two change together.
    if url is None:
        return None
    if smart_crop:
        return re.sub(_SIZE_SUFFIX_WITH_SMART_CROP, f"=w{size}-h{size}-p", url, count=1)
    return re.sub(_SIZE_SUFFIX, f"=w{size}-h{size}", url, count=1)
