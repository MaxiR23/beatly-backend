# 019. Image tiers are derived when serialized

Why `image` and `mosaic` are pydantic computed fields derived from the
already-rewritten thumbnail URL, the new import direction this creates, and
the rules that keep them out of anything written to the database (issue
#207).

## Context

Clients needed the same picture in three square sizes (192, 600 and 1320)
next to every `thumbnail_url`, which stays at 544 (the artist at 1200,
recents at 512 without smart crop). The URL rewrite already happens on read,
in Python, and nothing new is stored (`conventions.md`, "Image size").
About 20 places call `square_thumbnail_url`, 21 response models carry a
thumbnail and 5 carry a list of them.

The repo had no serialization-time computed field: 0 uses of
`computed_field`, `field_serializer` or `model_serializer`. And 0 of the 15
files in `models/` imported from `core/`.

## Decision

**Derive at serialization.** Each response model declares `image` (and
`mosaic` next to a list of thumbnails) as a pydantic `computed_field`. The
derivation lives in `core/thumbnails.py`, next to `square_thumbnail_url`, and
calls it three times with `smart_crop=True`. It takes the `thumbnail_url`
that is already rewritten, so the legacy fields do not change. `image` is
`null` when the URL is `null` or `""`; `mosaic` keeps the length and order of
the legacy list (its elements can never be `""`, the mosaic RPCs filter
them).

**New import direction.** `models/*` now imports `core/thumbnails.py`, which
imports `models/images.py`. `models/images.py` (the `ImageTiers` model) is a
leaf that imports nothing from the repo, so there is no cycle.

**Why not in the services.** Filling the field in each service would touch
every call site of `square_thumbnail_url`, and each new endpoint would have
to remember to do it. Worse, a cached Redis entry written before the tiers
existed would come back with the default (`null`) until it expired. Derived
on the way out, an old cached JSON validates without `image` and the tiers
are computed when it leaves; a new one carries `image`, which
`model_validate_json` ignores. Storing the tiers in the database was
rejected for the same reason as the 544 rule: it needs a migration, a
backfill and the rule duplicated in SQL.

**Models that are written to the database never carry a computed field.**
`TrackMetadata` is inherited by three requests and the services
`model_dump()` it straight into `tracks` and `play_events`; a computed field
would be a column that does not exist. A test
(`test_track_metadata_and_requests_carry_no_image`) fixes it for
`TrackMetadata` and the requests that inherit it.

**Recents.** `metadata` is free (`dict[str, Any]`), so it cannot hold a model
field. The write rule of migration 036 (512 without smart crop) is
untouched; `RecentEntity` derives `metadata.image` on read, in a copy of the
dict, and `null` when the stored value is not a non-empty string. The stored
row never gets `image` (an integration test checks it on the real row).

## Consequences

- Responses and cached entries grow: each `image` adds three URLs and each
  `mosaic` element three more. Measured with fixture-sized URLs, `image` is
  +(3L + 46) bytes and a 4-element `mosaic` +(12L + 163), with L the length
  of the 544 URL.
- Any new response model with a thumbnail must declare the computed field;
  any model that is dumped to the database must not.
- `image` is `null` for an empty `thumbnail_url`, so clients must check it.
- A URL without the size suffix is repeated as is in the three tiers; it is
  never an error.
