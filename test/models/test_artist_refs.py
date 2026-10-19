# test/models/test_artist_refs.py
#
# Tests for the shared artist reference types.
#
# Tested:
# - TrackArtists rejects an empty list with the track_artists_empty error
# - TrackArtists rejects a first artist with a missing, null or empty id with
#   the track_artists_primary_without_id error
# - An unlinked secondary artist is accepted and dumps as an explicit
#   id: None
# - A secondary without name is rejected by the model, not by the rule
# - A single linked artist is accepted
# - AlbumArtistRef keeps the old rule: an empty list and an unlinked first
#   artist are valid
# - Every song model requires artists (no default, so the rule cannot be
#   skipped)
#
# What is covered:
# - Both rejection types, the accepted shapes and the absence of defaults
#
# Run with: pytest test/models/test_artist_refs.py -v
#
# SEE: models/artist_refs.py

import pytest
from pydantic import BaseModel, ValidationError

from models.album import Album, AlbumTrack
from models.artist import ArtistSong
from models.artist_refs import AlbumArtistRef, TrackArtists
from models.public import PublicTrack
from models.search import SearchSong
from models.track import TrackRef


class _Holder(BaseModel):
    artists: TrackArtists


def _error_types(exc: ValidationError) -> list[str]:
    return [error["type"] for error in exc.errors(include_url=False)]


def test_track_artists_rejects_an_empty_list():
    with pytest.raises(ValidationError) as exc:
        _Holder(artists=[])

    assert _error_types(exc.value) == ["track_artists_empty"]


@pytest.mark.parametrize(
    "primary",
    [
        pytest.param({"name": "A"}, id="missing"),
        pytest.param({"id": None, "name": "A"}, id="none"),
        pytest.param({"id": "", "name": "A"}, id="empty"),
    ],
)
def test_track_artists_rejects_a_primary_without_id(primary):
    with pytest.raises(ValidationError) as exc:
        _Holder(artists=[primary, {"id": "a2", "name": "B"}])

    assert _error_types(exc.value) == ["track_artists_primary_without_id"]


def test_track_artists_accepts_an_unlinked_secondary():
    holder = _Holder(artists=[{"id": "a1", "name": "A"}, {"name": "B"}])

    assert holder.model_dump()["artists"] == [
        {"id": "a1", "name": "A"},
        {"id": None, "name": "B"},
    ]


def test_track_artists_rejects_a_secondary_without_name():
    with pytest.raises(ValidationError) as exc:
        _Holder(artists=[{"id": "a1", "name": "A"}, {"id": "a2"}])

    assert _error_types(exc.value) == ["missing"]


def test_track_artists_accepts_a_single_linked_artist():
    holder = _Holder(artists=[{"id": "a1", "name": "A"}])

    assert holder.model_dump()["artists"] == [{"id": "a1", "name": "A"}]


def test_album_artists_accept_an_empty_list_and_an_unlinked_first_artist():
    class _Album(BaseModel):
        artists: list[AlbumArtistRef]

    assert _Album(artists=[]).artists == []
    assert _Album(artists=[{"name": "A"}]).model_dump()["artists"] == [
        {"id": None, "name": "A"}
    ]


@pytest.mark.parametrize(
    ("model", "fields"),
    [
        (
            SearchSong,
            {
                "track_id": "t",
                "title": "T",
                "album": "A",
                "album_id": "a",
                "duration_seconds": 1,
                "thumbnail_url": "u",
            },
        ),
        (AlbumTrack, {"title": "T", "is_available": True, "track_number": 1}),
        (ArtistSong, {"title": "T"}),
        (TrackRef, {"track_id": "t", "title": "T"}),
        (PublicTrack, {"track_id": "t", "title": "T"}),
    ],
)
def test_song_models_require_artists(model, fields):
    with pytest.raises(ValidationError) as exc:
        model(**fields)

    assert _error_types(exc.value) == ["missing"]


def test_album_models_keep_artists_optional():
    album = Album(
        id="a",
        title="T",
        duration_seconds=1,
        tracks=[],
    )

    assert album.artists == []
