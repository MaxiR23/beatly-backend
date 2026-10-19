# INFO: Artist reference types shared by every model that carries a song.

from typing import Annotated

from pydantic import AfterValidator, BaseModel
from pydantic_core import PydanticCustomError

# Leaf module on purpose: album, artist, track, public, likes and playlists
# all need these types, and none of them can host them for the others
# without risking an import cycle. This file imports no models.*.

# Stable error types of the track artists rule. Services recognize the rule
# by these types (core/track_artist_rule.py) instead of repeating it, and
# they are PydanticCustomError rather than ValueError so a rejection by the
# rule cannot be confused with a malformed value (which would be value_error).
TRACK_ARTISTS_EMPTY = "track_artists_empty"
TRACK_ARTISTS_PRIMARY_WITHOUT_ID = "track_artists_primary_without_id"

# Readable reason per error type. No data, so it is safe to log.
TRACK_ARTISTS_REASONS = {
    TRACK_ARTISTS_EMPTY: "no artists",
    TRACK_ARTISTS_PRIMARY_WITHOUT_ID: "primary artist without id",
}


class TrackArtistRef(BaseModel):
    # A secondary artist can be mentioned with no link, which arrives with
    # no id and is dumped as an explicit id: null.
    id: str | None = None
    name: str


class AlbumArtistRef(BaseModel):
    # Same shape and same rule as before the track artists rule existed:
    # the id is optional and an empty list is valid. Kept apart so albums do
    # not depend on the rule for songs.
    id: str | None = None
    name: str


def _check_track_artists(artists: list[TrackArtistRef]) -> list[TrackArtistRef]:
    if not artists:
        raise PydanticCustomError(TRACK_ARTISTS_EMPTY, "track has no artists")
    # "First" is the order received. not id also covers an empty string.
    if not artists[0].id:
        raise PydanticCustomError(
            TRACK_ARTISTS_PRIMARY_WITHOUT_ID, "primary artist has no id"
        )
    return artists


# No min_length: it would fail with too_short before the validator runs, and
# the services could not recognize an empty list as the rule.
TrackArtists = Annotated[list[TrackArtistRef], AfterValidator(_check_track_artists)]
