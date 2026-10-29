# INFO: Pure functions that pick the dominant color of an image and adjust it
# to the dark theme of the app. No config and no I/O: the limits come in as
# arguments (see services/image_color_service.py).

import math
from collections import Counter
from io import BytesIO

from PIL import Image

# The image is shrunk to _SAMPLE x _SAMPLE with a box filter (deterministic,
# unlike the default sampling of draft()) and every pixel falls in a bucket
# of _BUCKET_BITS bits per channel. Pixels with every channel under
# _NEAR_BLACK or over _NEAR_WHITE are borders and backdrops, not the color of
# the image: they are ignored unless they are most of it (the non-extreme
# pixels are under _MIN_NON_EXTREME_SHARE of the sample).
_SAMPLE = 32
_BUCKET_BITS = 4
_NEAR_BLACK = 24
_NEAR_WHITE = 232
_MIN_NON_EXTREME_SHARE = 0.10
_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}

Rgb = tuple[int, int, int]


def hex_to_rgb(value: str) -> Rgb:
    return (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16))


def to_hex(rgb: Rgb) -> str:
    r, g, b = rgb
    return f"#{r:02x}{g:02x}{b:02x}"


def dominant_rgb(data: bytes, *, max_pixels: int) -> Rgb:
    # Raises PIL.UnidentifiedImageError for bytes that are not an image and
    # ValueError for a format outside _FORMATS or an image over max_pixels.
    # Image.open only reads the header, so the pixel check runs before any
    # decoding.
    with Image.open(BytesIO(data)) as img:
        if img.format not in _FORMATS:
            raise ValueError("unsupported image format")
        if img.width * img.height > max_pixels:
            raise ValueError("image has too many pixels")
        img.draft("RGB", (64, 64))
        sample = img.convert("RGB").resize((_SAMPLE, _SAMPLE), Image.Resampling.BOX)
        pixels = list(sample.get_flattened_data())

    non_extreme = [p for p in pixels if not _is_extreme(p)]
    pool = (
        non_extreme
        if len(non_extreme) >= _MIN_NON_EXTREME_SHARE * len(pixels)
        else pixels
    )
    shift = 8 - _BUCKET_BITS
    buckets = Counter((r >> shift, g >> shift, b >> shift) for r, g, b in pool)
    # Most populated bucket; a tie takes the lowest bucket tuple.
    winner = min(buckets, key=lambda bucket: (-buckets[bucket], bucket))
    members = [
        p for p in pool if (p[0] >> shift, p[1] >> shift, p[2] >> shift) == winner
    ]
    n = len(members)
    return (
        (sum(p[0] for p in members) + n // 2) // n,
        (sum(p[1] for p in members) + n // 2) // n,
        (sum(p[2] for p in members) + n // 2) // n,
    )


def _is_extreme(pixel: Rgb) -> bool:
    return max(pixel) < _NEAR_BLACK or min(pixel) > _NEAR_WHITE


def relative_luminance(rgb: Rgb) -> float:
    # WCAG 2.x relative luminance of an sRGB color.
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: Rgb, b: Rgb) -> float:
    lighter, darker = sorted(
        (relative_luminance(a), relative_luminance(b)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


# The lightness adjustment works in OKLCH (perceptual lightness, chroma and
# hue). The cube root is only ever applied to positive combinations of linear
# values >= 0, never to a negative number (in Python `(-x) ** (1 / 3)` is
# complex).
_CHROMA_SEARCH_STEPS = 30
_GAMUT_EPSILON = 1e-6


def _linear(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _encode(c: float) -> float:
    c = min(1.0, max(0.0, c))
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def _to_oklch(rgb: Rgb) -> tuple[float, float, float]:
    r, g, b = (_linear(v) for v in rgb)
    l_ = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m_ = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s_ = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    lightness = 0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_
    a = 1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_
    b2 = 0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_
    return lightness, math.hypot(a, b2), math.degrees(math.atan2(b2, a)) % 360


def _oklch_to_linear(
    lightness: float, chroma: float, hue: float
) -> tuple[float, float, float]:
    a = chroma * math.cos(math.radians(hue))
    b = chroma * math.sin(math.radians(hue))
    l_ = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (
        4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
        -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
        -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_,
    )


def _in_gamut(lightness: float, chroma: float, hue: float) -> bool:
    return all(
        -_GAMUT_EPSILON <= c <= 1 + _GAMUT_EPSILON
        for c in _oklch_to_linear(lightness, chroma, hue)
    )


def _from_oklch(lightness: float, chroma: float, hue: float) -> Rgb:
    # A chroma that does not fit in sRGB at this lightness is lowered by
    # bisection, at fixed lightness and hue (the gamut mapping of CSS Color 4).
    if not _in_gamut(lightness, chroma, hue):
        low, high = 0.0, chroma
        for _ in range(_CHROMA_SEARCH_STEPS):
            mid = (low + high) / 2
            if _in_gamut(lightness, mid, hue):
                low = mid
            else:
                high = mid
        chroma = low
    r, g, b = (
        round(_encode(c) * 255) for c in _oklch_to_linear(lightness, chroma, hue)
    )
    return (r, g, b)


def adjust_for_dark_theme(
    rgb: Rgb,
    *,
    background: Rgb,
    min_background_contrast: float,
    min_white_text_contrast: float,
) -> Rgb:
    # Moves only the OKLCH lightness, in steps of 0.001, with chroma and hue
    # fixed (a chroma that does not fit in sRGB at the new lightness is
    # lowered, at fixed lightness and hue), until the limit holds, checking
    # every step on the rounded 8-bit RGB.
    # Too dark to stand out from the background: lighten. Too light for
    # white text on top: darken. A color that meets both is returned as is.
    white = (255, 255, 255)
    lightness, chroma, hue = _to_oklch(rgb)
    step = round(lightness * 1000)

    def at(k: int) -> Rgb:
        return _from_oklch(k / 1000, chroma, hue)

    if contrast_ratio(rgb, background) < min_background_contrast:
        while contrast_ratio(rgb, background) < min_background_contrast and step < 1000:
            step += 1
            rgb = at(step)
    elif contrast_ratio(white, rgb) < min_white_text_contrast:
        while contrast_ratio(white, rgb) < min_white_text_contrast and step > 0:
            step -= 1
            rgb = at(step)
    return rgb
