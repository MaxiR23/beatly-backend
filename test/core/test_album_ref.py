# test/core/test_album_ref.py
#
# Tests for the shared mapping of an album row of the provider to an AlbumRef.
#
# Tested:
# - map_album_ref maps the row field by field
# - A row without artists (absent, None or []) gets [] without a fallback and
#   the fallback artist with one
# - A row with artists keeps them, even when the fallback is not among them
# - A row without year or audioPlaylistId gets both null
# - Empty or null thumbnails give a null thumbnail_url
# - The last thumbnail is requested at 544 x 544 with smart crop
# - A row missing browseId, title or thumbnails raises KeyError, and so does a
#   thumbnail without url (the caller translates it into a 502)
#
# What is covered:
# - core/album_ref.py on plain dicts, with no app and no provider
#
# Run with: pytest test/core/test_album_ref.py -v
#
# SEE: core/album_ref.py, models/album.py

import pytest

from core.album_ref import map_album_ref

_ROW = {
    "browseId": "MPREb_one",
    "title": "Album One",
    "artists": [{"id": "UCa", "name": "A"}],
    "year": "2020",
    "audioPlaylistId": "OLAK5uy_one",
    "thumbnails": [
        {"url": "https://example.com/small=w60-h60-l90-rj"},
        {"url": "https://example.com/large=w226-h226-l90-rj"},
    ],
}
_FALLBACK = {"id": "UCx", "name": "X"}


def _without(row, key):
    return {k: v for k, v in row.items() if k != key}


def _row_without_artists(mode):
    row = _without(_ROW, "artists")
    if mode == "none":
        row["artists"] = None
    elif mode == "empty":
        row["artists"] = []
    return row


def test_map_album_ref_maps_the_row_field_by_field():
    dumped = map_album_ref(_ROW).model_dump()

    assert dumped == {
        "id": "MPREb_one",
        "title": "Album One",
        "artists": [{"id": "UCa", "name": "A"}],
        "year": "2020",
        "audio_playlist_id": "OLAK5uy_one",
        "thumbnail_url": "https://example.com/large=w544-h544-p-l90-rj",
        "image": dumped["image"],
    }


@pytest.mark.parametrize("mode", ["absent", "none", "empty"])
def test_map_album_ref_without_artists_returns_an_empty_list(mode):
    assert map_album_ref(_row_without_artists(mode)).model_dump()["artists"] == []


@pytest.mark.parametrize("mode", ["absent", "none", "empty"])
def test_map_album_ref_without_artists_uses_the_fallback_artist(mode):
    ref = map_album_ref(_row_without_artists(mode), fallback_artist=_FALLBACK)

    assert ref.model_dump()["artists"] == [{"id": "UCx", "name": "X"}]


def test_map_album_ref_with_artists_ignores_the_fallback_artist():
    artists = [{"id": None, "name": "Various Artists"}]

    ref = map_album_ref({**_ROW, "artists": artists}, fallback_artist=_FALLBACK)

    assert ref.model_dump()["artists"] == artists


def test_map_album_ref_without_year_or_playlist_id_returns_null():
    row = _without(_without(_ROW, "year"), "audioPlaylistId")

    ref = map_album_ref(row)

    assert ref.year is None
    assert ref.audio_playlist_id is None


@pytest.mark.parametrize("thumbnails", [[], None])
def test_map_album_ref_with_empty_or_null_thumbnails_returns_null_thumbnail_url(
    thumbnails,
):
    assert map_album_ref({**_ROW, "thumbnails": thumbnails}).thumbnail_url is None


def test_map_album_ref_requests_the_last_thumbnail_at_544_smart_crop():
    assert (
        map_album_ref(_ROW).thumbnail_url
        == "https://example.com/large=w544-h544-p-l90-rj"
    )


@pytest.mark.parametrize("key", ["browseId", "title", "thumbnails"])
def test_map_album_ref_missing_a_required_key_raises_key_error(key):
    with pytest.raises(KeyError):
        map_album_ref(_without(_ROW, key))


def test_map_album_ref_thumbnail_without_url_raises_key_error():
    with pytest.raises(KeyError):
        map_album_ref({**_ROW, "thumbnails": [{"width": 1}]})
