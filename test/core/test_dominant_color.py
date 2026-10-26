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
#
# What is covered:
# - core/dominant_color.py, with synthetic images made with Pillow
#
# Run with: pytest test/core/test_dominant_color.py -v
#
# SEE: core/dominant_color.py, core/config.py

import colorsys
import re
from io import BytesIO

import pytest
from PIL import Image, UnidentifiedImageError

from core.dominant_color import (
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


def test_pure_red_is_darkened_keeping_its_hue():
    assert to_hex(_adjust((255, 0, 0))) == "#ee0000"


@pytest.mark.parametrize(
    "rgb",
    [(255, 0, 0), (255, 255, 0), (0, 255, 255), (255, 0, 255), (250, 240, 20)],
)
def test_adjustment_keeps_the_hue_within_rounding(rgb):
    before = colorsys.rgb_to_hls(*(v / 255 for v in rgb))[0]
    after = colorsys.rgb_to_hls(*(v / 255 for v in _adjust(rgb)))[0]

    assert abs(before - after) * 360 <= 4


def test_adjustment_honours_custom_limits():
    assert to_hex(_adjust((0, 0, 0), min_background_contrast=2.0)) == "#454545"
