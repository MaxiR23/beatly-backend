# INFO: Fetches a track's up-next queue, lyrics, related content and credits from the external provider.

from core.exceptions import NotFound
from core.search_provider import (
    ProviderResourceMissing,
    SearchProvider,
    provider_get_lyrics,
    provider_get_song_credits,
    provider_get_song_related,
    provider_get_watch_playlist,
)
from core.upstream import translate_upstream_errors
from models.album import AlbumRef
from models.artist import ArtistRef
from models.search import SearchArtistRef
from models.track import (
    TrackCredits,
    TrackCreditSection,
    TrackLyricLine,
    TrackLyrics,
    TrackLyricsResult,
    TrackRef,
    TrackRelated,
    TrackUpNext,
)

# The provider's videoType value for a proper studio recording of a song
# (as opposed to a music video, a user-uploaded clip, or an official-source
# but non-ATV upload). Only items carrying this value enter related.songs.
_ATV = "MUSIC_VIDEO_TYPE_ATV"

_UPNEXT_LIMIT = 50
# get_watch_playlist's continuation loop only runs while
# `limit - len(tracks) > 0`. With limit=1 and at least one track already
# parsed from the first response, that is never true, so /lyrics and
# /related -- which only need the browse ids off the first response --
# never pay for continuation requests for tracks they are going to discard.
_BROWSE_ONLY_LIMIT = 1

# The exact set of keys the library can set on a credits response
# (parsers/i18n.py, get_song_credit_section_map): tested by presence, never
# by truth -- the same rule _related_items already applies to
# audioPlaylistId and subscribers.
_TYPED_CREDIT_SECTIONS = (
    "performed_by",
    "written_by",
    "produced_by",
    "music_metadata_provided_by",
)


def get_track_upnext(provider: SearchProvider, track_id: str) -> TrackUpNext:
    with translate_upstream_errors():
        row = _watch_playlist(provider, track_id, limit=_UPNEXT_LIMIT)
        # row["tracks"] indexed: the library always builds this dict with
        # the four keys ("tracks", "playlistId", "lyrics", "related"), so
        # its absence is a layout change, not a documented branch.
        return TrackUpNext(tracks=[_map_upnext_track(t) for t in row["tracks"]])


def get_track_lyrics(provider: SearchProvider, track_id: str) -> TrackLyricsResult:
    with translate_upstream_errors():
        row = _watch_playlist(provider, track_id, limit=_BROWSE_ONLY_LIMIT)
        browse_id = row["lyrics"]
        if not browse_id:
            # watch["lyrics"] is None when get_tab_browse_ids found no
            # lyrics tab for this track (a documented branch, most
            # commonly an instrumental). Passing a falsy browseId to
            # get_lyrics raises the library's usage-error class, which is
            # deliberately excluded from PROVIDER_ERRORS and would surface
            # as a 500 instead of the 200 lyrics: null this case is.
            return TrackLyricsResult()

        raw = provider_get_lyrics(provider, browse_id)
        if raw is None:
            # get_lyrics returns None in its two documented branches when
            # the page it fetched carries no lyrics content.
            return TrackLyricsResult()

        return TrackLyricsResult(lyrics=_map_lyrics(raw))


def get_track_related(provider: SearchProvider, track_id: str) -> TrackRelated:
    with translate_upstream_errors():
        row = _watch_playlist(provider, track_id, limit=_BROWSE_ONLY_LIMIT)
        browse_id = row["related"]
        if not browse_id:
            # watch["related"] is None when get_tab_browse_ids found no
            # related tab for this track, the same documented branch as
            # lyrics above. Passing it to get_song_related would raise the
            # library's usage-error class.
            return TrackRelated()

        sections = provider_get_song_related(provider, browse_id)
        songs, artists, albums = _related_items(sections)
        return TrackRelated(songs=songs, artists=artists, albums=albums)


def get_track_credits(provider: SearchProvider, track_id: str) -> TrackCredits:
    with translate_upstream_errors():
        raw = _song_credits(provider, track_id)
        if raw is None:
            # The wrapper only returns None after the provider confirmed,
            # in a structured field, that the track exists: no credits is
            # a documented empty, not a swallowed failure.
            return TrackCredits()
        return _map_credits(raw)


def _watch_playlist(provider: SearchProvider, track_id: str, *, limit: int) -> dict:
    # One of the two try/except blocks in this service (the other is
    # _song_credits, below). Neither is the provider-failure try/except
    # translate_upstream_errors() already does: each translates a
    # structured signal from the provider (get_song's playabilityStatus)
    # into a domain exception, returns no value, swallows nothing and
    # re-raises with `from`. Called inside translate_upstream_errors(), and
    # NotFound passes straight through it untranslated.
    try:
        return provider_get_watch_playlist(provider, track_id, limit=limit)
    except ProviderResourceMissing as exc:
        raise NotFound("track_not_found") from exc


def _song_credits(provider: SearchProvider, track_id: str) -> dict | None:
    # Copied from _watch_playlist above, same five lines, same reasoning:
    # it is not the provider-failure try/except CLAUDE.md prohibits (that
    # one is still translate_upstream_errors()); it translates a structured
    # signal from the provider into a domain exception, returns no value,
    # swallows nothing and re-raises with `from`. NotFound passes straight
    # through translate_upstream_errors() untranslated
    # (core/upstream.py:9-10).
    try:
        return provider_get_song_credits(provider, track_id)
    except ProviderResourceMissing as exc:
        raise NotFound("track_not_found") from exc


def _map_upnext_track(row: dict) -> TrackRef:
    album, album_id = _song_album(row.get("album"))
    return TrackRef(
        track_id=row["videoId"],  # parse_watch_track indexes this
        title=row["title"],
        # The first item of the queue is the track being played, and it
        # arrives with no artists and no album: returned as-is, with
        # artists: [] and album/album_id null.
        artists=_artist_refs(row.get("artists")),
        album=album,
        album_id=album_id,
        duration_seconds=_duration_seconds(row),
        # "thumbnail", singular: parse_watch_track writes the track's
        # thumbnail list under that key, unlike related items below, which
        # use "thumbnails", plural, like album and artist rows do.
        thumbnail_url=_thumbnail_url(row["thumbnail"]),
    )


def _map_lyrics(raw: dict) -> TrackLyrics:
    # The three keys indexed: the library always builds the full TypedDict
    # in both branches (timestamped and plain).
    has_timestamps = bool(raw["hasTimestamps"])
    source = raw["source"]
    lines = (
        _timed_lines(raw["lyrics"]) if has_timestamps else _plain_lines(raw["lyrics"])
    )
    return TrackLyrics(has_timestamps=has_timestamps, source=source, lines=lines)


def _map_credits(raw: dict) -> TrackCredits:
    typed = {
        key: _credit_section(raw[key]) for key in _TYPED_CREDIT_SECTIONS if key in raw
    }
    # raw["other_sections"] indexed: the library initializes
    # credits = {"other_sections": []} before any navigation
    # (browsing.py:676), so its absence is a library change and has to be a
    # 502, not a silent default.
    return TrackCredits(
        **typed,
        other_sections=[_credit_section(s) for s in raw["other_sections"]],
    )


def _credit_section(section: dict) -> TrackCreditSection:
    # Both keys indexed: the library always builds the section dict with
    # the two of them (browsing.py:684-687). names is passed as-is, no
    # list(...) and no manual check: if the provider ever sent something
    # that is not a list of strings, pydantic raises ValidationError, which
    # core/upstream.py already translates to 502. A defensive list(...)
    # would turn a str into a list of characters and hide that failure.
    return TrackCreditSection(
        localized_title=section["localized_title"], names=section["data"]
    )


def _timed_lines(lines: list) -> list[TrackLyricLine]:
    # The provider returns LyricLine dataclasses, not dicts (measured).
    # Read with vars() on purpose, not by attribute: a type change on the
    # provider's side raises TypeError and a missing field raises KeyError,
    # both of which core/upstream.py translates to 502, while
    # line.text would raise AttributeError, which it does not translate
    # and would surface as a 500. Same reasoning as _thumbnail_url below.
    # id is discarded without being read.
    result = []
    for line in lines:
        data = vars(line)
        result.append(
            TrackLyricLine(
                text=data["text"], start_ms=data["start_time"], end_ms=data["end_time"]
            )
        )
    return result


def _plain_lines(text: str) -> list[TrackLyricLine]:
    # splitlines(), not split("\n"): the provider sends \r\n (measured),
    # and splitting on "\n" alone would leave a trailing \r on every line.
    # start_ms/end_ms stay None via the model's default, which is exactly
    # the explicit-null D2 requires. Blank lines are kept: they are the
    # provider's stanza breaks, not noise of our own.
    return [TrackLyricLine(text=line) for line in text.splitlines()]


def _related_items(
    sections: list[dict],
) -> tuple[list[TrackRef], list[ArtistRef], list[AlbumRef]]:
    # A section's "contents" is not always a list of dicts: the "About the
    # artist" section (DESCRIPTION_SHELF branch) sends contents as a str,
    # and parse_mixed_content can append None for an item whose page_type
    # matches none of its five known branches. Iterating either blindly
    # raises AttributeError, which core/upstream.py does not translate, so
    # it would surface as a 500. Only list contents are walked, and only
    # dict items inside them are classified.
    #
    # Classified by key presence, never by title (localized) or by order
    # (the provider changes it without notice), and tested with `in`, not
    # by truth: audioPlaylistId and subscribers come from a nav(..., True)
    # and can be None on a legitimate item.
    songs: list[TrackRef] = []
    artists: list[ArtistRef] = []
    albums: list[AlbumRef] = []

    for section in sections:
        contents = section.get("contents")
        if not isinstance(contents, list):
            continue
        for item in contents:
            if not isinstance(item, dict):
                continue

            if "videoId" in item:
                if item.get("videoType") == _ATV:
                    songs.append(_map_related_song(item))
            elif "audioPlaylistId" in item:
                albums.append(_map_album_ref(item))
            elif "subscribers" in item:
                # subscribers is discarded without being read, same as
                # services/artist_service.py.
                artists.append(
                    ArtistRef(
                        id=item["browseId"],
                        name=item["title"],
                        thumbnail_url=_thumbnail_url(item["thumbnails"]),
                    )
                )
            # Anything else (recommended playlists, episodes, other
            # unrecognized shapes) is skipped: these are the sections the
            # issue explicitly excludes.

    return songs, artists, albums


def _map_related_song(item: dict) -> TrackRef:
    album, album_id = _song_album(item.get("album"))
    return TrackRef(
        track_id=item["videoId"],
        title=item["title"],
        artists=_artist_refs(item.get("artists")),
        album=album,
        album_id=album_id,
        duration_seconds=item.get("duration_seconds"),
        # "thumbnails", plural, here -- unlike /upnext above.
        thumbnail_url=_thumbnail_url(item["thumbnails"]),
    )


def _map_album_ref(row: dict) -> AlbumRef:
    # Copied from services/artist_service.py, not imported: no service
    # imports another service today.
    return AlbumRef(
        id=row["browseId"],
        title=row["title"],
        artists=_artist_refs(row.get("artists")),
        year=row.get("year"),
        audio_playlist_id=row.get("audioPlaylistId"),
        thumbnail_url=_thumbnail_url(row["thumbnails"]),
    )


def _duration_seconds(row: dict) -> int | None:
    # A watch track only carries the numeric duration_seconds when
    # parse_song_runs finds a duration run in its longBylineText, which a
    # watch track usually spends on "Artist • Album • Year" instead. When
    # the key is absent, fall back to parsing "length" ("3:07"), the text
    # field parse_watch_track always sets (nullable).
    if "duration_seconds" in row:
        return row["duration_seconds"]
    return _seconds_from_length(row.get("length"))


def _seconds_from_length(length: str | None) -> int | None:
    # Must not raise on purpose: an int() over unexpected text would raise
    # ValueError, which core/upstream.py does not translate and would
    # surface as a 500 instead of 502. Returning None here does not hide
    # any failure -- the field is nullable by contract, so the response is
    # still correct.
    if not length:
        return None

    parts = length.split(":")
    if not all(part.isdigit() for part in parts):
        return None

    total = 0
    for part in parts:
        total = total * 60 + int(part)
    return total


def _artist_refs(rows: list[dict] | None) -> list[SearchArtistRef]:
    # The provider either omits the "artists" key or sets it to None when
    # nothing in the row parses as an artist run. Both normalize to [].
    #
    # Copied, not imported: no service imports another service today. The
    # third consumer is the one that extracts this into a shared module,
    # and this change is not it -- the rule stands as written.
    if not rows:
        return []
    return [SearchArtistRef(**row) for row in rows]


def _song_album(album: dict | None) -> tuple[str | None, str | None]:
    # album/album_id always travel together. The provider can send a name
    # with no id ({"name": <text>, "id": None}); losing the name in that
    # case is deliberate, so the pair is always coherent.
    if album is None or album["id"] is None:
        return None, None
    return album["name"], album["id"]


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
