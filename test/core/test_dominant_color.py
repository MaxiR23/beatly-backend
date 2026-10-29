# test/core/test_dominant_color.py
#
# Tests for the dominant color of an image and its adjustment to the dark
# theme.
#
# Tested:
# - to_hex writes #rrggbb in lowercase, seven characters
# - A solid red PNG is (255, 0, 0); the same red as a JPEG is the same on
#   every run
# - A black border does not win over the center; a mostly black image is
#   black; a tie takes the lowest bucket
# - Alpha and palette images decode
# - Bytes that are not an image raise, and so does an image over the pixel
#   cap, before decoding
# - Black is lifted to the minimum contrast against the background (#323232),
#   white is darkened until white text reaches 4.5 (#767676), a mid color is
#   unchanged, pure red keeps its hue, the hue holds within rounding, and the
#   limits are configurable
# - The adjustment works in OKLCH: the conversion matches the CSS Color 4
#   reference values and round-trips every 8-bit color of a sample; a pastel
#   keeps its chroma and hue (the nine measured covers, with their exact
#   hex); no measured control gets worse; a random sample meets both
#   contrast limits (default and custom) without rounding and never gains
#   chroma
#
# What is covered:
# - core/dominant_color.py, with synthetic images made with Pillow
#
# Run with: pytest test/core/test_dominant_color.py -v
#
# SEE: core/dominant_color.py, core/config.py

import random
import re
from io import BytesIO

import pytest
from PIL import Image, UnidentifiedImageError

from core.dominant_color import (
    _from_oklch,
    _to_oklch,
    adjust_for_dark_theme,
    contrast_ratio,
    dominant_rgb,
    hex_to_rgb,
    to_hex,
)

_BACKGROUND = hex_to_rgb("#0e0e0e")
_WHITE = (255, 255, 255)
_MAX_PIXELS = 4_194_304


def _png(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _solid(color, size=192, mode="RGB") -> Image.Image:
    return Image.new(mode, (size, size), color)


def _adjust(rgb, **overrides):
    limits = {
        "background": _BACKGROUND,
        "min_background_contrast": 1.5,
        "min_white_text_contrast": 4.5,
    }
    return adjust_for_dark_theme(rgb, **{**limits, **overrides})


def test_to_hex_is_lowercase_seven_characters():
    assert to_hex((255, 0, 171)) == "#ff00ab"
    assert re.fullmatch(r"#[0-9a-f]{6}", to_hex((0, 0, 0)))


def test_solid_red_png_is_ff0000():
    assert dominant_rgb(_png(_solid((255, 0, 0))), max_pixels=_MAX_PIXELS) == (
        255,
        0,
        0,
    )


def test_solid_red_jpeg_is_the_same_on_every_run():
    buffer = BytesIO()
    _solid((255, 0, 0)).save(buffer, format="JPEG", quality=90)
    data = buffer.getvalue()

    first = dominant_rgb(data, max_pixels=_MAX_PIXELS)
    second = dominant_rgb(data, max_pixels=_MAX_PIXELS)

    assert first == second == (254, 0, 0)


def test_a_black_border_does_not_win_over_the_center():
    image = _solid((0, 0, 0))
    image.paste(_solid((30, 90, 200), size=120), (36, 36))

    assert dominant_rgb(_png(image), max_pixels=_MAX_PIXELS) == (30, 90, 200)


def test_a_mostly_black_image_is_black():
    assert dominant_rgb(_png(_solid((0, 0, 0))), max_pixels=_MAX_PIXELS) == (0, 0, 0)


def test_a_tie_takes_the_lowest_bucket():
    image = _solid((200, 30, 30))
    image.paste(_solid((30, 200, 30), size=192).crop((0, 0, 96, 192)), (0, 0))

    assert dominant_rgb(_png(image), max_pixels=_MAX_PIXELS) == (30, 200, 30)


def test_alpha_and_palette_images_decode():
    alpha = _solid((10, 200, 100, 128), mode="RGBA")
    palette = _solid((255, 0, 0)).convert("P")

    assert dominant_rgb(_png(alpha), max_pixels=_MAX_PIXELS) == (10, 200, 100)
    assert dominant_rgb(_png(palette), max_pixels=_MAX_PIXELS) == (255, 0, 0)


def test_bytes_that_are_not_an_image_raise():
    with pytest.raises(UnidentifiedImageError):
        dominant_rgb(b"not an image", max_pixels=_MAX_PIXELS)


def test_too_many_pixels_raise_before_decoding():
    with pytest.raises(ValueError):
        dominant_rgb(_png(_solid((1, 2, 3), size=64)), max_pixels=64 * 64 - 1)


def test_black_is_lifted_to_the_minimum_contrast_against_the_background():
    result = _adjust((0, 0, 0))

    assert to_hex(result) == "#323232"
    assert contrast_ratio(result, _BACKGROUND) >= 1.5


def test_white_is_darkened_until_white_text_reaches_4_5():
    result = _adjust(_WHITE)

    assert to_hex(result) == "#767676"
    assert contrast_ratio(_WHITE, result) >= 4.5


def test_a_mid_color_is_unchanged():
    assert to_hex(_adjust((0x33, 0x66, 0xCC))) == "#3366cc"


def _hue_shift(before, after):
    return abs((_to_oklch(after)[2] - _to_oklch(before)[2] + 180) % 360 - 180)


def test_pure_red_is_darkened_keeping_its_hue():
    assert to_hex(_adjust((255, 0, 0))) == "#ee0000"
    assert _hue_shift((255, 0, 0), _adjust((255, 0, 0))) <= 0.5


@pytest.mark.parametrize(
    "rgb",
    [(255, 0, 0), (255, 255, 0), (0, 255, 255), (255, 0, 255), (250, 240, 20)],
)
def test_adjustment_keeps_the_hue_within_rounding(rgb):
    assert _hue_shift(rgb, _adjust(rgb)) <= 1.0


def test_oklch_matches_the_reference_values():
    for rgb, (lightness, chroma, hue) in {
        (255, 0, 0): (0.6280, 0.2577, 29.23),
        (0, 255, 0): (0.8664, 0.2948, 142.50),
        (0, 0, 255): (0.4520, 0.3132, 264.05),
    }.items():
        got = _to_oklch(rgb)
        assert got[0] == pytest.approx(lightness, abs=1e-3)
        assert got[1] == pytest.approx(chroma, abs=1e-3)
        assert got[2] == pytest.approx(hue, abs=0.05)
    white = _to_oklch((255, 255, 255))
    assert white[0] == pytest.approx(1.0, abs=1e-3)
    assert white[1] < 1e-6


def _random_colors(count):
    rng = random.Random(0)
    return [
        (rng.randrange(256), rng.randrange(256), rng.randrange(256))
        for _ in range(count)
    ]


def test_oklch_round_trips_every_8_bit_color_of_the_sample():
    colors = _random_colors(2000) + [
        (255, 0, 0),
        (0, 255, 0),
        (0, 0, 255),
        (255, 255, 255),
        (0, 0, 0),
    ]

    assert [c for c in colors if _from_oklch(*_to_oklch(c)) != c] == []


def test_a_pastel_keeps_its_chroma_and_hue():
    before = hex_to_rgb("#f8b6c4")
    after = _adjust(before)

    assert to_hex(after) == "#a16673"
    assert _to_oklch(after)[1] == pytest.approx(0.078, abs=0.005)
    assert _hue_shift(before, after) <= 1.1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("#f8b6c4", "#a16673", id="swag-ii"),
        pytest.param("#fcb7d8", "#a26382", id="lover"),
        pytest.param("#ab93a0", "#87707d", id="thank-u-next"),
        pytest.param("#fee3c7", "#8a7259", id="sweetener"),
        pytest.param("#e3d3b8", "#83755c", id="harrys-house"),
        pytest.param("#f7d6b9", "#8e7156", id="emails-i-cant-send"),
        pytest.param("#c8d6e3", "#6c7884", id="honeymoon"),
        pytest.param("#e8c7aa", "#8f7156", id="manana-sera-bonito"),
        pytest.param("#d8dae9", "#747683", id="damn"),
    ],
)
def test_every_measured_pastel_keeps_its_chroma_and_hue(raw, expected):
    before = hex_to_rgb(raw)
    after = _adjust(before)
    chroma_before = _to_oklch(before)[1]

    assert to_hex(after) == expected
    assert abs(_to_oklch(after)[1] - chroma_before) <= 0.005
    assert _hue_shift(before, after) <= (1.2 if chroma_before >= 0.03 else 2.5)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("#0675aa", "#0675aa"),
        ("#574577", "#574577"),
        ("#6c5874", "#6c5874"),
        ("#e01707", "#e01707"),
        ("#d61f2c", "#d61f2c"),
        ("#8bcf00", "#578300"),
        ("#ffffff", "#767676"),
        ("#a7a7a7", "#767676"),
        ("#000000", "#323232"),
        ("#000101", "#2c3433"),
    ],
)
def test_no_measured_control_gets_worse(raw, expected):
    assert to_hex(_adjust(hex_to_rgb(raw))) == expected


@pytest.mark.parametrize(
    ("background", "white", "count"),
    [(1.5, 4.5, 2000), (2.0, 4.5, 500), (4.2, 4.5, 500), (1.0, 7.0, 500)],
)
def test_every_result_of_a_random_sample_meets_both_limits(background, white, count):
    for rgb in _random_colors(count):
        result = _adjust(
            rgb, min_background_contrast=background, min_white_text_contrast=white
        )

        assert contrast_ratio(_WHITE, result) >= white
        assert contrast_ratio(result, _BACKGROUND) >= background


def test_a_random_sample_never_gains_chroma_and_keeps_the_hue():
    for rgb in _random_colors(2000):
        result = _adjust(rgb)
        chroma_before, chroma_after = _to_oklch(rgb)[1], _to_oklch(result)[1]

        assert chroma_after <= chroma_before + 0.005
        if result != rgb and min(chroma_before, chroma_after) >= 0.05:
            assert _hue_shift(rgb, result) <= 2.0


def test_adjustment_honours_custom_limits():
    assert to_hex(_adjust((0, 0, 0), min_background_contrast=2.0)) == "#454545"
