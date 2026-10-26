# test/models/test_images.py
#
# Tests for the image tiers the response models derive from their legacy
# thumbnail fields.
#
# Tested:
# - Each of the 21 response models with a thumbnail_url dumps image with the
#   192 / 600 / 1320 tiers derived from it
# - Each of the 16 whose thumbnail_url is nullable dumps image: null, with the
#   key present, when it is null
# - An empty thumbnail_url gives image: null on all 21
# - The 5 models with a mosaic dump one tier set per URL, in order, and []
#   gives []
# - A Public model built from an internal model_dump() keeps its image, because
#   it recomputes it from its own thumbnail_url
# - Every image, every mosaic element and metadata.image of a recent carry
#   color: null outside a request, and carry the color of the scope inside one
# - TrackMetadata and the requests that are dumped to the database carry no
#   image
#
# What is covered:
# - The computed image / mosaic fields of models/*.py
#
# Run with: pytest test/models/test_images.py -v
#
# SEE: models/images.py, core/thumbnails.py

import contextvars

import pytest

from core.image_color_scope import open_image_color_scope
from models.activity import LogPlayRequest, RecentEntity
from models.album import Album, AlbumRef
from models.artist import Artist, ArtistRef, ArtistRelease, ArtistSong
from models.genres import GenrePlaylist, GenrePlaylistListItem, GenrePlaylistTrack
from models.library import AddLibraryItemRequest, LibraryEntry, LibraryItem
from models.likes import AddLikeRequest, Like, TrackMetadata
from models.playlists import (
    AddPlaylistTrackRequest,
    PlaylistDetailWithCovers,
    PlaylistListItem,
    PlaylistPageTrack,
)
from models.public import (
    PublicAlbum,
    PublicArtist,
    PublicGenrePlaylist,
    PublicPlaylist,
    PublicPlaylistTrack,
    PublicTrack,
)
from models.search import SearchAlbum, SearchArtist, SearchSong
from models.track import TrackRef

_URL = "https://lh3.googleusercontent.com/abc"
_SUFFIXED = f"{_URL}=w544-h544-p-l90-rj"
_TIERS = {
    "small": f"{_URL}=w192-h192-p-l90-rj",
    "medium": f"{_URL}=w600-h600-p-l90-rj",
    "large": f"{_URL}=w1320-h1320-p-l90-rj",
    "color": None,
}
_ARTISTS = [{"id": "a1", "name": "Artist"}]
_ALBUM_ARTISTS = [{"id": "a1", "name": "Artist"}]

# Minimum valid kwargs, without thumbnail_url. Each entry is
# (model, kwargs, nullable thumbnail_url).
_MODELS = [
    (SearchArtist, {"id": "x", "name": "n"}, True),
    (
        SearchSong,
        {
            "track_id": "t",
            "title": "T",
            "artists": _ARTISTS,
            "album": "A",
            "album_id": "a",
            "duration_seconds": 1,
        },
        False,
    ),
    (SearchAlbum, {"id": "x", "title": "T", "artists": _ALBUM_ARTISTS}, True),
    (
        Album,
        {
            "id": "x",
            "title": "T",
            "artists": _ALBUM_ARTISTS,
            "track_count": 0,
            "duration_seconds": 1,
            "tracks": [],
        },
        True,
    ),
    (AlbumRef, {"id": "x", "title": "T", "artists": _ALBUM_ARTISTS}, True),
    (ArtistSong, {"track_id": "t", "title": "T", "artists": _ARTISTS}, True),
    (ArtistRelease, {"id": "x", "title": "T"}, True),
    (ArtistRef, {"id": "x", "name": "n"}, True),
    (Artist, {"id": "x", "name": "n"}, True),
    (TrackRef, {"track_id": "t", "title": "T", "artists": _ARTISTS}, True),
    (
        Like,
        {
            "track_id": "t",
            "title": "T",
            "artists": _ARTISTS,
            "album": "A",
            "album_id": "a",
            "duration_seconds": 1,
            "created_at": "c",
            "updated_at": "u",
        },
        False,
    ),
    (
        LibraryItem,
        {
            "kind": "album",
            "external_id": "e",
            "title": "T",
            "source": "external",
            "added_at": "a",
            "updated_at": "u",
        },
        True,
    ),
    (
        LibraryEntry,
        {
            "kind": "album",
            "id": "e",
            "title": "T",
            "source": "external",
            "thumbnail_urls": [],
        },
        True,
    ),
    (
        GenrePlaylist,
        {"id": "x", "title": "T", "track_count": 0},
        True,
    ),
    (
        GenrePlaylistTrack,
        {
            "track_id": "t",
            "title": "T",
            "artists": _ARTISTS,
            "album": "A",
            "album_id": "a",
            "duration_seconds": 1,
            "position": 1,
        },
        False,
    ),
    (
        PlaylistPageTrack,
        {
            "track_id": "t",
            "title": "T",
            "artists": _ARTISTS,
            "album": "A",
            "album_id": "a",
            "duration_seconds": 1,
            "position": 1,
        },
        False,
    ),
    (
        PublicAlbum,
        {
            "id": "x",
            "title": "T",
            "artists": _ALBUM_ARTISTS,
            "duration_seconds": 1,
            "tracks": [],
        },
        True,
    ),
    (PublicArtist, {"id": "x", "name": "n"}, True),
    (PublicTrack, {"track_id": "t", "title": "T", "artists": _ARTISTS}, True),
    (
        PublicPlaylistTrack,
        {
            "track_id": "t",
            "title": "T",
            "artists": _ARTISTS,
            "album": "A",
            "album_id": "a",
            "duration_seconds": 1,
            "position": 1,
        },
        False,
    ),
    (
        PublicGenrePlaylist,
        {
            "id": "x",
            "title": "T",
            "owner": {"type": "app"},
            "track_count": 0,
            "has_more": False,
            "total_duration_seconds": 0,
            "tracks": [],
        },
        True,
    ),
]
_IDS = [m[0].__name__ for m in _MODELS]


def _build(model, kwargs, url):
    return model(**kwargs, thumbnail_url=url)


def test_there_are_21_image_models():
    assert len(_MODELS) == 21


@pytest.mark.parametrize(("model", "kwargs", "nullable"), _MODELS, ids=_IDS)
def test_every_image_model_derives_tiers_from_its_thumbnail_url(
    model, kwargs, nullable
):
    dumped = _build(model, kwargs, _SUFFIXED).model_dump()

    assert dumped["thumbnail_url"] == _SUFFIXED
    assert dumped["image"] == _TIERS


@pytest.mark.parametrize(
    ("model", "kwargs"),
    [(m, k) for m, k, nullable in _MODELS if nullable],
    ids=[m.__name__ for m, _, nullable in _MODELS if nullable],
)
def test_nullable_thumbnail_url_gives_image_none_with_the_key_present(model, kwargs):
    dumped = _build(model, kwargs, None).model_dump()

    assert "image" in dumped
    assert dumped["image"] is None


@pytest.mark.parametrize(("model", "kwargs", "nullable"), _MODELS, ids=_IDS)
def test_empty_thumbnail_url_gives_image_none(model, kwargs, nullable):
    assert _build(model, kwargs, "").model_dump()["image"] is None


_MOSAIC_MODELS = [
    (
        PlaylistListItem,
        {
            "id": "x",
            "owner_id": "o",
            "title": "T",
            "is_public": False,
            "created_at": "c",
            "updated_at": "u",
        },
        "thumbnail_urls",
    ),
    (
        PlaylistDetailWithCovers,
        {
            "id": "x",
            "owner_id": "o",
            "title": "T",
            "is_public": False,
            "created_at": "c",
            "updated_at": "u",
            "total_count": 0,
            "total_duration_seconds": 0,
        },
        "thumbnail_urls",
    ),
    (
        GenrePlaylistListItem,
        {"id": "x", "title": "T", "track_count": 0},
        "thumbnail_urls",
    ),
    (
        LibraryEntry,
        {"kind": "album", "id": "e", "title": "T", "source": "external"},
        "thumbnail_urls",
    ),
    (
        PublicPlaylist,
        {
            "id": "x",
            "title": "T",
            "owner": {"type": "user"},
            "track_count": 0,
            "has_more": False,
            "total_duration_seconds": 0,
            "tracks": [],
        },
        "thumbnails",
    ),
]


@pytest.mark.parametrize(
    ("model", "kwargs", "field"),
    _MOSAIC_MODELS,
    ids=[m[0].__name__ for m in _MOSAIC_MODELS],
)
def test_every_mosaic_model_derives_one_tier_per_url_in_order(model, kwargs, field):
    other = "https://example.com/b.png"

    one = model(**kwargs, **{field: [_SUFFIXED]}).model_dump()
    four = model(**kwargs, **{field: [_SUFFIXED, other, _SUFFIXED, other]}).model_dump()
    none = model(**kwargs, **{field: []}).model_dump()

    assert one["mosaic"] == [_TIERS]
    assert [m["small"] for m in four["mosaic"]] == [
        _TIERS["small"],
        other,
        _TIERS["small"],
        other,
    ]
    assert none["mosaic"] == []


def test_public_models_built_from_an_internal_dump_keep_their_image():
    # Built the way services/public_service.py builds them: from the
    # model_dump() of the internal model, which already carries image.
    album = Album(
        id="x",
        title="T",
        artists=_ALBUM_ARTISTS,
        track_count=0,
        duration_seconds=1,
        tracks=[],
        thumbnail_url=_SUFFIXED,
    )
    artist = Artist(id="x", name="n", thumbnail_url=_SUFFIXED)
    track = TrackRef(track_id="t", title="T", artists=_ARTISTS, thumbnail_url=_SUFFIXED)

    public_album = PublicAlbum(
        **album.model_dump(
            exclude={"audio_playlist_id", "other_versions", "related_recommendations"}
        )
    )
    public_artist = PublicArtist(**artist.model_dump(exclude={"related"}))
    public_track = PublicTrack(**track.model_dump())

    assert public_album.model_dump()["image"] == _TIERS
    assert public_artist.model_dump()["image"] == _TIERS
    assert public_track.model_dump()["image"] == _TIERS


def test_track_metadata_and_requests_carry_no_image():
    # A computed field here would be a column that does not exist the day a
    # service model_dump()s the model straight into tracks / play_events.
    metadata = {
        "title": "T",
        "artists": _ARTISTS,
        "album": "A",
        "album_id": "a",
        "duration_seconds": 1,
        "thumbnail_url": _SUFFIXED,
    }
    models = [
        TrackMetadata(**metadata),
        AddLikeRequest(track_id="t", **metadata),
        LogPlayRequest(track_id="t", **metadata),
        AddPlaylistTrackRequest(track_id="t", **metadata),
        AddLibraryItemRequest(
            kind="album",
            external_id="e",
            title="T",
            source="external",
            thumbnail_url=_SUFFIXED,
        ),
    ]

    for model in models:
        assert "image" not in model.model_dump()


def _all_tier_dicts(dumped):
    found = []

    def walk(node):
        if isinstance(node, dict):
            if {"small", "medium", "large", "color"} <= node.keys():
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(dumped)
    return found


def _recent():
    return RecentEntity(
        entity_type="album",
        entity_id="x",
        metadata={"title": "T", "thumbnail_url": _SUFFIXED},
        played_at="2026-01-01T00:00:00Z",
    )


def test_every_image_and_mosaic_carries_color_null_outside_a_request():
    dumps = [
        _build(model, kwargs, _SUFFIXED).model_dump() for model, kwargs, _ in _MODELS
    ]
    dumps += [
        model(**kwargs, **{field: [_SUFFIXED, _SUFFIXED]}).model_dump()
        for model, kwargs, field in _MOSAIC_MODELS
    ]
    dumps.append(_recent().model_dump())

    tiers = [t for dumped in dumps for t in _all_tier_dicts(dumped)]

    assert len(tiers) >= len(_MODELS) + 2 * len(_MOSAIC_MODELS) + 1
    assert all(t["color"] is None for t in tiers)


def test_inside_a_scope_the_image_is_noted_and_takes_the_loaded_color():
    # In a copied context, so the scope never leaks into another test.
    def inside():
        scope = open_image_color_scope()
        scope.colors[_URL] = "#ee0000"
        image = _build(*_MODELS[0][:2], _SUFFIXED).model_dump()["image"]
        recent = _recent().model_dump()["metadata"]["image"]
        return scope, image, recent

    scope, image, recent = contextvars.copy_context().run(inside)

    assert image["color"] == "#ee0000"
    assert recent["color"] == "#ee0000"
    assert scope.seen == {_URL: _TIERS["small"]}
