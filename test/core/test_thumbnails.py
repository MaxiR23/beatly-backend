# test/core/test_thumbnails.py
#
# Tests for the square thumbnail URL rewrite.
#
# Tested:
# - smart_crop=True rewrites the size suffix to a square with exactly one -p
# - A flag such as -pd is never taken for -p (and is dropped)
# - Options the CDN rejects next to -p (-s, -dc...) are dropped, the ones in
#   the allow list (l<n>, rj, rw) are kept, and the rule is idempotent
# - The new rule leaves image_key unchanged
# - smart_crop=True is idempotent for the provider's URL format
# - smart_crop=False rewrites only the size and leaves what follows intact
# - smart_crop=False is idempotent
# - Only the first size suffix is rewritten
# - A URL without the size suffix is returned as is
# - Non-ASCII digits are not a size suffix
# - None returns None
# - image_tiers derives the 192 / 600 / 1320 tiers (smart crop) from a 544,
#   1200 or 512 URL, repeats a URL without suffix in the three, gives None
#   for None and "", and keeps the keys small, medium, large, color in order
# - color is None outside a request (no image color scope)
# - image_key is the URL up to the first size suffix, the same for every size
#   of one image, and the whole URL when there is no suffix
# - mosaic_tiers keeps the length and the order, and [] gives []
#
# What is covered:
# - Smart crop and plain rewrite, exact -p flag, idempotence, first
#   occurrence only, no host filter, URL without suffix, None, ASCII digits
#   only
#
# Run with: pytest test/core/test_thumbnails.py -v
#
# SEE: core/thumbnails.py, models/images.py, services/artist_service.py,
# services/activity_service.py

import pytest

from core.thumbnails import (
    LARGE_IMAGE_SIZE,
    MEDIUM_IMAGE_SIZE,
    SMALL_IMAGE_SIZE,
    image_key,
    image_tiers,
    mosaic_tiers,
    square_thumbnail_url,
)

_URL = "https://lh3.googleusercontent.com/abc"


@pytest.mark.parametrize(
    ("size", "url", "expected"),
    [
        (1200, f"{_URL}=w2880-h1200-l90-rj", f"{_URL}=w1200-h1200-p-l90-rj"),
        (
            1200,
            "https://yt3.googleusercontent.com/abc=w2880-h1200-p-l90-rj",
            "https://yt3.googleusercontent.com/abc=w1200-h1200-p-l90-rj",
        ),
        (544, f"{_URL}=w226-h226-p-l90-rj", f"{_URL}=w544-h544-p-l90-rj"),
        (1200, f"{_URL}=w2880-h1200-p", f"{_URL}=w1200-h1200-p"),
        (1200, f"{_URL}=w2880-h1200", f"{_URL}=w1200-h1200-p"),
        (
            1200,
            "https://example.com/abc=w10-h10-l90",
            "https://example.com/abc=w1200-h1200-p-l90",
        ),
    ],
)
def test_smart_crop_rewrites_the_size_suffix_to_a_square_with_p(size, url, expected):
    assert square_thumbnail_url(url, size, smart_crop=True) == expected


_CLASH_CASES = [
    (f"{_URL}=w60-h60-s-l90-rj", f"{_URL}=w{{n}}-h{{n}}-p-l90-rj"),
    (f"{_URL}=w60-h60-p-s-l90-rj", f"{_URL}=w{{n}}-h{{n}}-p-l90-rj"),
    (f"{_URL}=w120-h120-l90-rj-dcqTWQzhkK", f"{_URL}=w{{n}}-h{{n}}-p-l90-rj"),
    (f"{_URL}=w60-h60-p-dcAB12-l90-rj", f"{_URL}=w{{n}}-h{{n}}-p-l90-rj"),
    (f"{_URL}=w60-h60", f"{_URL}=w{{n}}-h{{n}}-p"),
    (f"{_URL}=w60-h60-p", f"{_URL}=w{{n}}-h{{n}}-p"),
    (f"{_URL}=w60-h60-l94-rw", f"{_URL}=w{{n}}-h{{n}}-p-l94-rw"),
    (f"{_URL}=w512-h512-p-l94-rw", f"{_URL}=w{{n}}-h{{n}}-p-l94-rw"),
    (f"{_URL}=w512-h512-p-l90-rj", f"{_URL}=w{{n}}-h{{n}}-p-l90-rj"),
]
_CLASH_SIZES = [192, 544, 600, 1320]


@pytest.mark.parametrize("size", _CLASH_SIZES)
@pytest.mark.parametrize(("url", "template"), _CLASH_CASES)
def test_smart_crop_never_emits_an_option_that_clashes_with_p(url, template, size):
    assert square_thumbnail_url(url, size, smart_crop=True) == template.format(n=size)


@pytest.mark.parametrize("size", _CLASH_SIZES)
@pytest.mark.parametrize(("url", "template"), _CLASH_CASES)
def test_smart_crop_is_idempotent_on_the_clash_cases(url, template, size):
    once = square_thumbnail_url(url, size, smart_crop=True)

    assert square_thumbnail_url(once, size, smart_crop=True) == once
    assert square_thumbnail_url(once, 192, smart_crop=True) == square_thumbnail_url(
        url, 192, smart_crop=True
    )


@pytest.mark.parametrize(("url", "template"), _CLASH_CASES)
def test_image_key_is_unchanged_by_the_new_rule(url, template):
    rewritten = square_thumbnail_url(url, 192, smart_crop=True)

    assert image_key(rewritten) == image_key(url)


def test_smart_crop_never_takes_pd_for_p():
    url = f"{_URL}=w2880-h1200-pd-l90-rj"
    expected = f"{_URL}=w1200-h1200-p-l90-rj"

    once = square_thumbnail_url(url, 1200, smart_crop=True)

    assert once == expected
    assert square_thumbnail_url(once, 1200, smart_crop=True) == expected


@pytest.mark.parametrize(
    "url",
    [
        f"{_URL}=w2880-h1200-l90-rj",
        f"{_URL}=w2880-h1200-p-l90-rj",
        f"{_URL}=w1200-h1200-p-l90-rj",
        f"{_URL}=w226-h226-p-l90-rj",
    ],
)
def test_smart_crop_is_idempotent(url):
    once = square_thumbnail_url(url, 1200, smart_crop=True)

    assert square_thumbnail_url(once, 1200, smart_crop=True) == once


def test_smart_crop_leaves_an_already_square_url_unchanged():
    url = f"{_URL}=w1200-h1200-p-l90-rj"

    assert square_thumbnail_url(url, 1200, smart_crop=True) == url


_PLAIN_CASES = [
    (f"{_URL}=w120-h120-l90-rj", f"{_URL}=w512-h512-l90-rj"),
    (f"{_URL}=w544-h544-l90-rj", f"{_URL}=w512-h512-l90-rj"),
    (f"{_URL}=w540-h225-p-l90-rj", f"{_URL}=w512-h512-p-l90-rj"),
    (f"{_URL}=w540-h225-pd-l90-rj", f"{_URL}=w512-h512-pd-l90-rj"),
]


@pytest.mark.parametrize(("url", "expected"), _PLAIN_CASES)
def test_without_smart_crop_rewrites_only_the_size(url, expected):
    assert square_thumbnail_url(url, 512, smart_crop=False) == expected


@pytest.mark.parametrize(("url", "expected"), _PLAIN_CASES)
def test_without_smart_crop_is_idempotent(url, expected):
    once = square_thumbnail_url(url, 512, smart_crop=False)

    assert square_thumbnail_url(once, 512, smart_crop=False) == once


@pytest.mark.parametrize(
    ("smart_crop", "size", "expected"),
    [
        (True, 1200, f"{_URL}=w1200-h1200-p-l90-rj?x=w10-h10"),
        (False, 512, f"{_URL}=w512-h512-l90-rj?x=w10-h10"),
    ],
)
def test_only_the_first_size_suffix_is_rewritten(smart_crop, size, expected):
    url = f"{_URL}=w2880-h1200-l90-rj?x=w10-h10"

    assert square_thumbnail_url(url, size, smart_crop=smart_crop) == expected


@pytest.mark.parametrize("smart_crop", [True, False])
@pytest.mark.parametrize(
    "url", ["https://example.com/a1.png", "https://lh3.googleusercontent.com/abc=s120"]
)
def test_url_without_size_suffix_is_returned_as_is(smart_crop, url):
    assert square_thumbnail_url(url, 512, smart_crop=smart_crop) == url


@pytest.mark.parametrize("smart_crop", [True, False])
def test_non_ascii_digits_are_not_a_size_suffix(smart_crop):
    url = "https://example.com/abc=w١٢-h١٢-l90"

    assert square_thumbnail_url(url, 512, smart_crop=smart_crop) == url


@pytest.mark.parametrize("smart_crop", [True, False])
def test_none_returns_none(smart_crop):
    assert square_thumbnail_url(None, 512, smart_crop=smart_crop) is None


# --- Image tiers -------------------------------------------------------------


def test_tier_sizes_are_192_600_1320():
    assert (SMALL_IMAGE_SIZE, MEDIUM_IMAGE_SIZE, LARGE_IMAGE_SIZE) == (192, 600, 1320)


def test_image_tiers_rewrites_a_544_url_to_192_600_1320_with_smart_crop():
    tiers = image_tiers(f"{_URL}=w544-h544-p-l90-rj")

    assert tiers.model_dump() == {
        "small": f"{_URL}=w192-h192-p-l90-rj",
        "medium": f"{_URL}=w600-h600-p-l90-rj",
        "large": f"{_URL}=w1320-h1320-p-l90-rj",
        "color": None,
    }


def test_image_tiers_from_the_1200_artist_url():
    tiers = image_tiers(f"{_URL}=w1200-h1200-p")

    assert tiers.large == f"{_URL}=w1320-h1320-p"
    assert tiers.small == f"{_URL}=w192-h192-p"


def test_image_tiers_from_a_512_recents_url_without_p_adds_smart_crop():
    tiers = image_tiers(f"{_URL}=w512-h512-l90-rj")

    assert tiers.model_dump() == {
        "small": f"{_URL}=w192-h192-p-l90-rj",
        "medium": f"{_URL}=w600-h600-p-l90-rj",
        "large": f"{_URL}=w1320-h1320-p-l90-rj",
        "color": None,
    }


def test_image_tiers_of_a_url_without_suffix_repeats_it_in_all_three():
    url = "https://example.com/a1.png"

    assert image_tiers(url).model_dump() == {
        "small": url,
        "medium": url,
        "large": url,
        "color": None,
    }


@pytest.mark.parametrize("url", [None, ""])
def test_image_tiers_of_none_and_empty_string_is_none(url):
    assert image_tiers(url) is None


def test_image_tiers_never_takes_pd_for_p():
    tiers = image_tiers(f"{_URL}=w544-h544-pd-l90")

    assert tiers.small == f"{_URL}=w192-h192-p-l90"


def test_image_tiers_keys_are_small_medium_large_in_order():
    tiers = image_tiers(f"{_URL}=w544-h544-p")

    assert list(tiers.model_dump()) == ["small", "medium", "large", "color"]


def test_mosaic_tiers_keeps_length_and_order():
    urls = [f"{_URL}=w544-h544-p-l90-rj", "https://example.com/b.png", f"{_URL}=w1-h1"]

    mosaic = mosaic_tiers(urls)

    assert [m.small for m in mosaic] == [
        f"{_URL}=w192-h192-p-l90-rj",
        "https://example.com/b.png",
        f"{_URL}=w192-h192-p",
    ]
    assert len(mosaic) == 3


def test_mosaic_tiers_of_empty_list_is_empty():
    assert mosaic_tiers([]) == []


def test_tiers_without_a_scope_have_color_none():
    assert image_tiers(f"{_URL}=w544-h544-p").color is None
    assert [tiers.color for tiers in mosaic_tiers([f"{_URL}=w60-h60"])] == [None]


@pytest.mark.parametrize(
    "suffix", ["=w544-h544-p-l90-rj", "=w192-h192-p", "=w60-h60-l90-rj", "=w512-h512"]
)
def test_image_key_is_the_same_for_every_size_of_an_image(suffix):
    assert image_key(f"{_URL}{suffix}") == _URL


def test_image_key_of_a_url_without_suffix_is_the_url():
    url = "https://i.ytimg.com/vi/abc/hq720.jpg"

    assert image_key(url) == url
