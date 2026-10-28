# INFO: Rewrites the size suffix of a provider CDN image URL to a square and
# derives the small, medium and large image tiers from it.

import re

from core.image_color_scope import observe_image
from models.images import ImageTiers

# The provider's CDN reads the image size from a suffix such as
# =w544-h544-p-l90-rj (width, height, -p = smart crop). [0-9] and not \d,
# because \d also accepts non-ASCII digits. The options after the size are
# the dash-separated tokens that follow it.
_SIZE_SUFFIX = r"=w[0-9]+-h[0-9]+"
_SIZE_AND_OPTIONS = re.compile(_SIZE_SUFFIX + r"((?:-[^-/?#=]+)*)")

# The options kept next to -p: quality (l<n>) and the output format (rj, rw).
# An allow list and not a deny list on purpose: the CDN rejects -p followed by
# some options (-s, -dc...; measured), and an option nobody measured is
# dropped, which gives a working URL, instead of kept, which could give a
# broken one.
_KEPT_OPTION = re.compile(r"^(?:l[0-9]+|r[jw])$")


def _smart_crop_suffix(size: int, options: str) -> str:
    kept = [t for t in options.split("-") if _KEPT_OPTION.match(t)]
    return f"=w{size}-h{size}-p" + "".join(f"-{t}" for t in kept)


def square_thumbnail_url(url: str | None, size: int, *, smart_crop: bool) -> str | None:
    # Replaces only the first occurrence, with no host filter. A URL without
    # the size suffix is returned as is, never as an error; None gives None.
    # smart_crop=True leaves exactly one -p after the size (adds it when
    # missing, does not duplicate it), keeps only the options in
    # _KEPT_OPTION after it and drops the rest, because the CDN answers 400
    # to -p-s and to -p-dc.... It is idempotent.
    # smart_crop=False does not touch what follows, -p included: that branch
    # is the rule of migration 036 (see _normalize_thumbnail_url in
    # services/activity_service.py) and the two change together.
    if url is None:
        return None
    if smart_crop:
        return _SIZE_AND_OPTIONS.sub(
            lambda m: _smart_crop_suffix(size, m.group(1)), url, count=1
        )
    return re.sub(_SIZE_SUFFIX, f"=w{size}-h{size}", url, count=1)


# The three sizes every image is offered in, always square with smart crop.
# Small: rows and the mini player. Medium: carousels, grids and tiles.
# Large: the player and heroes.
SMALL_IMAGE_SIZE = 192
MEDIUM_IMAGE_SIZE = 600
LARGE_IMAGE_SIZE = 1320


def image_key(url: str) -> str:
    # The identity of an image for its color: the URL up to the first size
    # suffix, so every size of one image shares a key. A URL without the
    # suffix is its own key.
    return re.split(_SIZE_SUFFIX, url, maxsplit=1)[0]


def _tiers(url: str) -> ImageTiers:
    # Derives from the already rewritten legacy URL (544, 1200 or 512): the
    # rewrite touches only the first size suffix and is idempotent, so the
    # starting size does not matter.
    small = square_thumbnail_url(url, SMALL_IMAGE_SIZE, smart_crop=True)
    return ImageTiers(
        small=small,
        medium=square_thumbnail_url(url, MEDIUM_IMAGE_SIZE, smart_crop=True),
        large=square_thumbnail_url(url, LARGE_IMAGE_SIZE, smart_crop=True),
        color=observe_image(image_key(url), small),
    )


def image_tiers(url: str | None) -> ImageTiers | None:
    # None and "" give None. A URL without the size suffix gives the same URL
    # in the three tiers, never an error.
    if not url:
        return None
    return _tiers(url)


def mosaic_tiers(urls: list[str]) -> list[ImageTiers]:
    # Same length and order as urls. No None inside: the mosaic RPCs filter
    # out null and '' (see services/playlist_service.py).
    return [_tiers(url) for url in urls]
