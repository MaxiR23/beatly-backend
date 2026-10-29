# 023. The image color adjustment keeps chroma and hue, and the stored key carries the algorithm version

Why the adjustment of the cover color to the dark theme works in OKLCH and
keeps chroma and hue instead of the HLS saturation, why the contrast of white
text is still measured on the pure color, and why the key stored in
`image_colors` carries the version of the algorithm (issue #215, built on
#211, #212 and #213).

## Context

The adjustment of ADR 021 moved the HLS lightness and kept the HLS
saturation. For a pastel that is the wrong thing to keep: to darken a light
pink until white text reads on it, the same saturation on a darker lightness
is a much more chromatic color. Measured on 36 real covers: SWAG II
`#f8b6c4` became `#e91643`, OKLCH chroma 0.078 -> 0.232, hue shifted 14.4
degrees; Lover, Sweetener and emails i can't send did the same, and on the
dark side Metallica `#000101` became `#003939`. The near-white filter of
`dominant_rgb` is not the cause (SWAG II has 0 % near-white pixels): the
cause is keeping the HLS saturation. The paragraph "The color is adjusted
to the dark theme" of ADR 021 no longer holds.

The stored colors are those of the old algorithm. Changing the algorithm
without touching them leaves every already-seen image with the old color
forever, because a row with a color is never claimed again.

## Decision

**The adjustment works in OKLCH.** The perceptual lightness L moves in steps
of 0.001, with chroma C and hue h fixed, checking each step on the rounded
8-bit RGB, with the same two branches and the same limits as before
(lighten against the background, darken for white text). If at the new
lightness the chroma does not fit in sRGB, it is lowered by bisection (30
iterations) at fixed lightness and hue, which is the gamut mapping of CSS
Color 4 and of the tonal palettes of Material. The conversion is a few
private functions of `core/dominant_color.py` with only `math`: no new
dependency.

Rejected alternatives, with the measurement: HLS (the current one, the
problem above); scaling the chroma with the lightness (SWAG II `#946b74`,
Honeymoon `#6f777f`: it turns the pastels gray); a color library
(`requirements.txt` does not change).

Over 20 000 random colors (seed 0): 0 failures of either contrast limit; the
chroma never rises more than 0.0019; the hue shifts at most 1.84 degrees
where the chroma is 0.05 or more; time per color 0.81 ms on average and 6.6
ms at most (the yellows), against 0.16 ms of HLS. It is not bounded: a color
is computed once per image and version, in a pool thread, after a download
that costs far more and after the response was sent. Pure red `#ee0000`,
white `#767676` and black `#323232` are the same as before. SWAG II becomes
`#a16673` (chroma 0.078 -> 0.078).

**The contrast of white text is measured on the pure color, 4.5.** The
client is not in this repo and the contract says "under white text".
Relaxing it (3:1 of large text, or measuring at the end of a gradient) is
changing the default of `IMAGE_COLOR_MIN_WHITE_TEXT_CONTRAST`; it is a
decision of the owner with the client at hand and this record does not take
it.

**The stored key carries the version.** The key that reaches the database is
`v<version>:` plus the image key (`v2:https://...`); 1 was the HLS
algorithm, stored without a prefix. It is applied only at the storage edge of
`services/image_color_service.py`, in the three places where the key leaves
for the database (the read, the claim and the write of `_compute_one`),
with a dictionary from stored key to key to come back. The rest (the
scope, `observe_image`, `image_key` of `core/thumbnails.py`, the logs) keeps
the bare key: the scope and `_tiers()` look up by `image_key(url)`, and the
digest of the log is of the bare key. An old row never matches
`= ANY (p_keys)`: the image looks unseen, is claimed and a new row is
inserted. The RPC treat the key as opaque text and do not change. A column
`algorithm_version` was rejected because it asks for changing both RPC. The
rule: any change that changes the stored color of an image bumps
`_COLOR_ALGORITHM_VERSION`.

## Replaces

This record replaces, from `021`: the paragraph "The color is adjusted to
the dark theme" on "Only the lightness changes (hue and saturation stay)" and
on the figure "the hue drifted at most 3.43 degrees... for saturations above
0.05" (now 1.84 degrees). The black `#323232`, white `#767676`, red `#ee0000`
and the 2.0 / `#454545` example still hold. And the sentence "The key is the
image, not the URL": the image key is still that, but the one stored in
`image_colors` carries the version prefix.

## Consequences

- On deploy every color goes back to `null` until a request computes it again
  (up to 10 per request plus the main image, in the background); the
  `max-age` is the short one of `IMAGE_COLOR_PENDING_MAX_AGE` meanwhile.
  A burst of downloads to the provider CDN, bounded by the pool (4 per
  process).
- Old and new instances live together during the deploy without stepping on
  each other: each reads and writes its own rows.
- Rolling back the code before `045` recovers the old colors; after it, the
  old code computes them again.
- `045` deletes the orphan rows after the deploy. It is optional: without it
  they are harmless. `045` deletes every key that does not start with
  `v2:`, so a `v3` will need its own cleanup that names `v2:`, and `045`
  must not be applied after a `v3` deploy.
- Honeymoon and DAMN. end up grayer than before because that is their real
  chroma (0.024 and 0.021). The almost-gray covers (chroma under 0.012) drift
  up to 7.5 degrees of hue with no visible difference.
- Computing a color is about 5 times more expensive on average than with HLS,
  still small against the download.
