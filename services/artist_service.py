# INFO: Fetches an artist's page (top songs, albums, singles and related artists) from the external provider.


from core.album_ref import map_album_ref
from core.cache import CacheClient, cache_get, cache_key, cache_set, cache_ttl
from core.search_provider import SearchProvider, provider_get_artist
from core.thumbnails import square_thumbnail_url
from core.track_artist_rule import build_track
from core.upstream import translate_upstream_errors
from models.artist import Artist, ArtistRef, ArtistRelease, ArtistSong

# TTL for a cached artist page: the provider rotates its carousels, so it
# is treated as less stable than album/lyrics/credits.
_ARTIST_TTL_SECONDS = 12 * 60 * 60

# Side of the square image requested from the provider's CDN, always with
# smart crop. The artist image goes to 1200; related artists go to 544, the
# largest variant the provider sends for them today (asking for more would
# only be an upscale); songs and singles go to 544 too. Albums take the same
# 544 from core/album_ref.py.
_THUMBNAIL_SIZE = 544
_ARTIST_IMAGE_SIZE = 1200
_RELATED_IMAGE_SIZE = 544


def get_artist(
    provider: SearchProvider, cache: CacheClient, artist_id: str
) -> tuple[Artist, int | None]:
    key = cache_key("artist", artist_id)
    # The read stays outside translate_upstream_errors(): a ValidationError
    # from a stale cached value is a cache failure, not a 502.
    cached = cache_get(cache, key, Artist)
    if cached is not None:
        return cached, cache_ttl(cache, key)

    artist = _fetch_artist(provider, artist_id)
    cache_set(cache, key, artist, ttl=_ARTIST_TTL_SECONDS)
    return artist, _ARTIST_TTL_SECONDS


def _fetch_artist(provider: SearchProvider, artist_id: str) -> Artist:
    with translate_upstream_errors():
        row = provider_get_artist(provider, artist_id)
        # The provider sends no artists in the albums of an artist page (386
        # of 386 measured in #229), so the artist of the page is used, with
        # the same id as Artist.id. An album that does carry artists keeps
        # them.
        owner = {"id": artist_id, "name": row["name"]}

        return Artist(
            id=artist_id,  # the provider's channelId is a different (video)
            # channel, not the id that was requested; never read it back.
            name=row["name"],
            thumbnail_url=square_thumbnail_url(
                _thumbnail_url(row["thumbnails"]), _ARTIST_IMAGE_SIZE, smart_crop=True
            ),
            songs=[
                song
                for song in (_map_song(s) for s in _song_rows(row))
                if song is not None
            ],
            albums=[
                map_album_ref(a, fallback_artist=owner)
                for a in _section_rows(row, "albums")
            ],
            singles=[_map_release(s) for s in _section_rows(row, "singles")],
            related=[_map_related(r) for r in _section_rows(row, "related")],
        )


def _section_rows(row: dict, key: str) -> list[dict]:
    # A category the artist has no section for is absent from the response
    # entirely rather than empty: parse_channel_contents only creates it
    # `if len(data) > 0`. That branch is documented, so it returns [].
    #
    # Past that the section is indexed, never .get()-chained: a section
    # that stopped being a dict raises TypeError, and one without
    # "results" raises KeyError, and core/upstream.py translates both to
    # 502. A .get() chain would raise AttributeError, which it does not
    # translate and which would surface as a 500, and an inner default
    # would hide the same layout change behind a silently empty list --
    # the one shape docs/api/artists.md promises never to return.
    return row.get(key, {"results": []})["results"]


def _song_rows(row: dict) -> list[dict]:
    # songs is the one section whose "results" key is genuinely optional:
    # the provider always creates the category dict but only fills it when
    # the page carries that shelf ("API sometimes does not return songs").
    # Merging over the default covers that branch and the category being
    # absent, while a section that stopped being a dict still raises
    # TypeError -> 502, which a .get() chain would not.
    return ({"results": []} | row.get("songs", {}))["results"]


def _map_song(row: dict) -> ArtistSong | None:
    album, album_id = _song_album(row.get("album"))
    return build_track(
        "artist song",
        ArtistSong,
        {
            "track_id": row.get("videoId"),
            "title": row["title"],
            "artists": _artist_refs(row.get("artists")),
            "album": album,
            "album_id": album_id,
            # duration_seconds: the key is only added `if duration:`.
            "duration_seconds": row.get("duration_seconds"),
            "thumbnail_url": square_thumbnail_url(
                _thumbnail_url(row["thumbnails"]), _THUMBNAIL_SIZE, smart_crop=True
            ),
        },
    )


def _song_album(album: dict | None) -> tuple[str | None, str | None]:
    # album/album_id always travel together. The provider can send a name
    # with no id ({"name": <text>, "id": None}); losing the name in that
    # case is deliberate, so the pair is always coherent.
    if album is None or album["id"] is None:
        return None, None
    return album["name"], album["id"]


def _map_release(row: dict) -> ArtistRelease:
    return ArtistRelease(
        id=row["browseId"],
        title=row["title"],
        # year/type: both conditional keys from
        # _parse_album_single_subtitle, absent when not applicable.
        year=row.get("year"),
        type=row.get("type"),
        thumbnail_url=square_thumbnail_url(
            _thumbnail_url(row["thumbnails"]), _THUMBNAIL_SIZE, smart_crop=True
        ),
    )


def _map_related(row: dict) -> ArtistRef:
    # subscribers is deliberately not read: it is not exposed by this
    # endpoint.
    return ArtistRef(
        id=row["browseId"],
        name=row["title"],
        thumbnail_url=square_thumbnail_url(
            _thumbnail_url(row["thumbnails"]), _RELATED_IMAGE_SIZE, smart_crop=True
        ),
    )


def _artist_refs(rows: list[dict] | None) -> list[dict]:
    # The provider either omits the "artists" key or sets it to None when
    # nothing in the row parses as an artist run: parse_playlist_item
    # builds a song with artists=None when no flex column resolves to one.
    # Both normalize to [].
    #
    # Copied, not imported: the mapping helpers are private to each service
    # and each copy carries the reasons of its own domain. The album
    # reference mapping was the exception and was extracted to
    # core/album_ref.py (#229) because its three copies were identical. The
    # rows go to the model raw: it builds the artist references and applies
    # the track artists rule (models/artist_refs.py).
    return rows or []


def _thumbnail_url(thumbnails: list[dict] | None) -> str | None:
    # The largest resolution is the last entry. Returning None instead of
    # indexing blindly matters: an empty or absent list would raise
    # IndexError, which core/upstream.py does not translate, so it would
    # surface as a 500 instead of the 502 every other provider failure
    # gets.
    #
    # Copied, not imported, for the same reason as _artist_refs above.
    if not thumbnails:
        return None

    return thumbnails[-1]["url"]
