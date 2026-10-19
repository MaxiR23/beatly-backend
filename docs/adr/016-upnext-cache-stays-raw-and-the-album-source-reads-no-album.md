# 016. The upnext cache stays raw, and the album source reads no album

Why `GET /tracks/{track_id}/upnext` caches the raw queue and filters it
per caller afterwards, why the `album` source filters by `album_id`
without asking the provider for the album, and why the response carries
`private, max-age` instead of `no-cache` (issue #201).

## Context

`upnext` is one of the eight operations whose provider response is cached
(ADR 005), under a key that depends only on the track id, so every user
who asks for the same track shares one entry. #201 makes the endpoint
leave out what the caller already heard in the recent window, and, when
the request names a source (`album`, `playlist`, `genre_playlist`,
`liked`), the tracks of that source. That is the first endpoint served
from the provider cache whose answer depends on who asks.

The `album` source needs to know which queue items belong to the album.
The queue items already carry `album_id`, so one way is to compare it with
the `source_id` of the request. The other is to ask the provider for the
album and compare track ids.

The front pins the suggestions per source (front #84). With `no-cache` the
client could revalidate in the middle of a source and the list would
change under the user.

## Decision

**The cache keeps the raw, shared queue; personalization runs after it.**
`get_track_upnext` and `_fetch_upnext` are unchanged: they read and write
the unfiltered queue. `get_personal_upnext` takes that value and builds a
new `TrackUpNext` for the caller, and never writes the cache. A filtered
list is never stored, so one user's history cannot reach another through
the cache. This is the same rule as ADR 005 (the cache lives in the
services, keyed per operation of the provider), extended to say that the
cached value is the provider's data and nothing derived from the caller.
The user-data reads happen outside the cache, on the caller's database
client, and a failure there is a 502 or 504 with the cache untouched.

**The `album` source filters by `album_id`, with no call to the provider.**
The repo owner decided not to ask the provider anything for the source. A
call to the album would turn a nonexistent album into a 502 (ADR 002),
and that breaks the rule that a well-formed source that does not exist is
ignored (200, only the played tracks are filtered). Comparing `album_id`
cannot fail: a nonexistent album simply matches nothing.

**The response is `private, max-age=<N>`, not `no-cache`.** `private`
because the body depends on the caller; `max-age` because of front #84,
described above. `no-cache` stays for user data (`/library`, `/likes`,
...), where the client must always revalidate.

## Consequences

- Queue items whose `album_id` is null are not filtered by an `album`
  source, even if they belong to that album. Accepted by the owner as the
  price of not calling the provider.
- Any future personalization of a cached provider operation follows the
  same shape: cache raw, filter after, never write the filtered value.
- Two users asking for the same track within the TTL pay one provider
  call and get different lists.
- The client that pins a list per source keeps it for the remaining TTL
  of the entry, not longer.
