# 027. The album reference mapping is shared in core, with an artist fallback

Where the mapping from an album row of the provider to an `AlbumRef` lives,
how an album with no artist gets the owner of the artist page, and which
mapping helpers stay private to each service (issue #229). It does not
supersede an earlier record; it corrects the reason ADR 026 gives for adding
the owner of the page to the edition keys (see the Corrections in this
directory's README).

## Context

Three services (artist, album and track) each carried an identical private
`_map_album_ref`. #229 needed one change in that mapping: an album of an
artist page that carries no artist must carry the owner of the page, so a
recommendation built from it has an artist to show and to deduplicate by.
Changing three copies by hand invites them to diverge, and one of them is
used by pages where that fallback is wrong: an album in `/album` or in
`/tracks/{id}/related` must keep `artists: []` when the provider sends none.

The services also hold other mapping helpers (`_artist_refs`,
`_thumbnail_url`, the track mappers). Their copies are not identical in
intent: some carry a domain reason of their own, and `search_service` has
diverged on purpose (a thumbnail without a `url` is `None` there and a
`KeyError`, so a 502, in the others).

## Decision

**`map_album_ref` lives in `core/album_ref.py`**, and the three services
import it. It is extracted because the three copies were identical and had no
domain reason to differ. It follows `core/track_artist_rule.py` and
`core/thumbnails.py`: a rule shared by several domains lives in `core/`. It
has no try/except; the `translate_upstream_errors()` of each caller still
turns a missing key into a 502.

**The owner of the page is a keyword-only `fallback_artist` parameter**, used
only when the row carries no artist at all. A row with artists keeps them,
even when the owner is not among them. Only `artist_service` passes it,
because only the artist page knows whose album it is. `/album` and
`/tracks/{id}/related` call it without it and their output does not change.

**The other mapping helpers stay private to each service.** The rule is: a
helper is extracted to `core/` when its copies are identical and have no
domain reason of their own; otherwise each service keeps its copy.
`_artist_refs` and `_thumbnail_url` stay copied, and `search_service` keeps
its own mapping.

**An album recommended by an album seed with no artist is not a candidate.**
With the fallback, the albums of an artist page always have an artist, but
the related albums of an album page do not, and without an artist an edition
key cannot be built. The filter is in `from_albums` of
`services/recommendations_service.py`.

## Alternatives rejected

- **Completing the artist afterwards with `model_copy`.** Every caller would
  need to know the mapped model has no artist and patch it, and the three
  services would still be free to differ.
- **A fallback that also replaces existing artists.** It would rewrite what
  the provider sent; `/artist` and `/public/artist` would stop showing the
  real artists of a collaboration.
- **Passing the fallback in `/album` and `/tracks/{id}/related` too.** Those
  pages have no owner to give, and their documented `artists: []` would
  change.
- **Extracting every mapping helper to `core/`.** It would merge copies that
  differ for a reason, and `search_service` could not keep its rule for a
  thumbnail without `url`.
- **Keeping the albums without artists as candidates, deduplicated by id
  only.** Two editions of the same album could reach the same list.

## Consequences

- Changing `map_album_ref` changes `/artist`, `/public/artist`, `/album` and
  `/tracks/{id}/related` at once; its tests pin the four outputs.
- A set stored before #229 can hold `artists: []` for an album of an artist
  page until it expires or is regenerated (12 h). That is why the edition
  keys still add the owner of the page.
- Measured on the album pages of the recommendations, 2 of 290 related
  albums carried no artist; those are no longer recommended, so a list can
  be one item shorter.
- The helpers that stay copied can still drift. That is accepted: the rule
  above says when a copy is worth sharing, and a new service that needs the
  same mapping imports the one in `core/`.
