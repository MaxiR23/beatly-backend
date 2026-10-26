# INFO: Pure functions that pick the dominant color of an image and adjust it
# to the dark theme of the app. No config and no I/O: the limits come in as
# arguments (see services/image_color_service.py).

import colorsys
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


def adjust_for_dark_theme(
    rgb: Rgb,
    *,
    background: Rgb,
    min_background_contrast: float,
    min_white_text_contrast: float,
) -> Rgb:
    # Moves only the lightness (hue and saturation stay), in steps of 0.001,
    # until the limit holds, checking every step on the rounded 8-bit RGB.
    # Too dark to stand out from the background: lighten. Too light for
    # white text on top: darken. A color that meets both is returned as is.
    white = (255, 255, 255)
    h, lightness, s = colorsys.rgb_to_hls(*(v / 255 for v in rgb))
    step = round(lightness * 1000)

    def at(k: int) -> Rgb:
        r, g, b = colorsys.hls_to_rgb(h, k / 1000, s)
        return (round(r * 255), round(g * 255), round(b * 255))

    if contrast_ratio(rgb, background) < min_background_contrast:
        while contrast_ratio(rgb, background) < min_background_contrast and step < 1000:
            step += 1
            rgb = at(step)
    elif contrast_ratio(white, rgb) < min_white_text_contrast:
        while contrast_ratio(white, rgb) < min_white_text_contrast and step > 0:
            step -= 1
            rgb = at(step)
    return rgb
