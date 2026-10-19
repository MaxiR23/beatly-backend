# 015. Every song carries artists, and the first one has an id

Why a song that does not meet the track artists rule is rejected, left
out, answered 404 or answered 502 depending on where it comes from, why
album artists keep the old rule, and why the database CHECK is created
`NOT VALID` (issue track-artists). The rule itself is in
`docs/api/conventions.md`, "Track artists".

## Context

Two rules about song artists contradicted each other. The writes and
reads of the database (`TrackMetadata`, `PlaylistTrack`,
`PlaylistPageTrack`) required `list[TrackArtist]` with `id: str` on every
artist, and `min_length=1`. The five song models built from the provider
(`SearchSong`, `AlbumTrack`, `ArtistSong`, `TrackRef`, `PublicTrack`)
declared `list[SearchArtistRef] = Field(default_factory=list)`: optional
`id`, empty list allowed.

The consequence for a user: a real song with a secondary artist that has no
id could be listed and played, but could not be saved to Likes or to a
playlist, because the write demanded an id on every artist. Meanwhile a
song with no artists, or whose first artist had no id, could not be linked
to an artist page. The data came in through two doors, the provider lists
and the writes (`POST /likes`, the playlist track endpoints,
`POST /plays`), each with its own idea of the rule.

Whether an endpoint should fail or skip depends on what the endpoint is
for, so one behavior for all of them was not an option.

## Decision

**One type, in a leaf module.** `TrackArtists` in `models/artist_refs.py`
is a list of `TrackArtistRef` plus an after-validator: not empty, and
`artists[0].id` is truthy (an empty string counts as missing). It lives
in a module that imports no `models.*`, so that album, artist, track,
public, likes and playlists can all use it and none of them has to host it
for the others. The five song models declare `artists:
TrackArtists` with no default, so the rule cannot be skipped by omission.

**The errors are `PydanticCustomError`**, with the stable types
`track_artists_empty` and `track_artists_primary_without_id`, not
`ValueError`. A `ValueError` becomes `value_error`, which a malformed
value also produces; the services need to tell "only the rule failed"
from "the row is broken". `core/track_artist_rule.py` does that:
`artist_rule_reason()` gives a reason only when every error of the
`ValidationError` is one of the two types, and `build_track()` catches
only `ValidationError`, re-raises anything that is not just the rule, and
returns `None` for the one song it drops.

**What each kind of endpoint does:**

- *Guarded writes* (likes, playlist tracks, bulk, plays): 422
  `invalid_request`, nothing written; one bad song rejects a whole bulk.
- *Lists built from the provider* (search, album, artist, upnext after the
  first item, related, and the public equivalents): the song is left out
  and one warning is logged with the kind and the reason, never the data.
  `track_number` in an album keeps the provider's numbering, so the
  numbers have gaps. If every song was left out, the list is `[]` in a
  200. In `/search`, rows dropped only by the rule never make the 502 of
  ADR 012: a 502 needs every row to have fallen for format.
- *A single requested song* (`GET /public/tracks/{id}` and the first item
  of `GET /tracks/{id}/upnext`): 404 `track_not_found`, never cached. On
  `/upnext` the head is looked at before the rest of the queue is built.
- *Reads from the database* (likes, playlists, genre playlists, public
  playlists): 502 `upstream_error` if a stored row breaks the rule. This
  is the repo owner's decision (options A and D of the issue): the
  existing `translate_upstream_errors()` already gives it, no read is
  changed, and no migration `042` deletes the rows that break it. The
  affected rows get fixed by saving them again from the app.

**Album artists keep the old rule** (`AlbumArtistRef`: optional `id`, the
list may be empty). The repo owner asked not to change album artists
without their approval, so that is left for another issue. The two types
are separate so albums do not depend on the rule for songs.

**The CHECK `tracks_artists_primary_has_id` (migration `041`) is
`NOT VALID`.** It backs the first-artist rule at the database, for the
writers outside the repo. Rows already break it: the owner counted 61 rows of
`genre_playlist_tracks` whose track has a single artist with a null id
(they can be fewer distinct tracks, since a track can be in several genre
playlists; the genre script lives outside this repo). `VALIDATE` would
fail on them. `NOT VALID` enforces it on every
`INSERT` and `UPDATE` from then on and does not scan the rows already
there, with a brief lock and no scan. `VALIDATE CONSTRAINT` waits until
those rows are rewritten.

## Consequences

- `track_not_found` is no longer only "the id does not exist at the
  provider": it also answers a track that exists and does not meet the
  rule (see the correction to 003 in `docs/adr/README.md`).
- A stored row that breaks the rule makes the whole read endpoint
  respond 502 until the row is rewritten. Accepted, and why `041` exists:
  no new such row can be written.
- Until genre playlists are rebuilt, the genre script fails on any track
  whose first artist has no id, and a batch upsert fails as a whole.
- A song the provider lists badly disappears from a list without the
  client being told; the only trace is the warning in the server log.
- `VALIDATE CONSTRAINT` on `tracks_artists_primary_has_id` is pending.

Alternatives rejected:

- *`min_length=1` on the list.* It fails with `too_short` before the
  validator runs, and the services could not recognize an empty list as
  the rule.
- *Repeating the check in each service (`artists[0]`).* Five places that
  drift; the type is the single definition.
- *Failing every list with a 502 when one song breaks the rule.* One
  degraded song would take the whole page down.
- *Migration `042` deleting the stored rows that break the rule.*
  Irreversible, it deletes users' likes and playlist entries, and a
  client's local cache would keep the deleted like.
