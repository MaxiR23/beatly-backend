# test/services/test_ingest_service.py
#
# Tests for the service behind POST /ingest/genres and POST /ingest/releases.
#
# Tested:
# - _rpc_params is pure: sort_order 1..n inside each genre, position 1..n
#   inside each playlist in the order received, a null description and
#   category go as null, the tracks rows are the model dump with the id of
#   every artist explicit
# - The same request gives the same parameters
# - An unknown slug raises NotFound("genre_not_found") before any rpc and its
#   log line carries the count, never the slug
# - The count returned by the function must be the number of tracks sent; a
#   bool or another number raises UpstreamError and is logged with both numbers
# - A database failure while reading genres or calling the function becomes
#   UpstreamError, a timeout UpstreamTimeout
#
# - Releases: _release_rpc_params is pure, numbers position 1..n from the
#   order of the list and keeps the seven keys; ingest_releases makes one rpc
#   and returns the count; a count that is not the one sent (a bool included)
#   raises UpstreamError and is logged with both numbers; a database failure
#   becomes UpstreamError and a timeout UpstreamTimeout
#
# What is covered:
# - The service alone, with a fake database. The route and the real function
#   are in test/routes/test_ingest.py and
#   test/integration/test_ingest_integration.py
#
# Run with: pytest test/services/test_ingest_service.py -v
#
# SEE: services/ingest_service.py, models/ingest.py

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from postgrest.exceptions import APIError

from core.exceptions import NotFound, UpstreamError, UpstreamTimeout
from models.ingest import GenreIngestRequest, ReleaseIngestRequest
from services.ingest_service import (
    _release_rpc_params,
    _rpc_params,
    ingest_genres,
    ingest_releases,
)


def _request(with_text=True):
    playlist = {
        "source_playlist_id": "PLa",
        "title": "A",
        "tracks": ["t2", "t1"],
    }
    if with_text:
        playlist["description"] = "Desc"
        playlist["category"] = "Cat"
    return GenreIngestRequest.model_validate(
        {
            "genres": [
                {
                    "slug": "rock",
                    "playlists": [
                        playlist,
                        {"source_playlist_id": "PLb", "title": "B", "tracks": ["t1"]},
                    ],
                },
                {
                    "slug": "pop",
                    "playlists": [
                        {"source_playlist_id": "PLc", "title": "C", "tracks": ["t2"]}
                    ],
                },
            ],
            "tracks": [
                {
                    "track_id": track_id,
                    "title": track_id,
                    "artists": [{"id": "UC1", "name": "A"}, {"name": "B"}],
                    "album": "Al",
                    "album_id": "MPREb_1",
                    "duration_seconds": 100,
                    "thumbnail_url": "https://example.com/c.jpg",
                }
                for track_id in ("t1", "t2")
            ],
        }
    )


def _db(known=("rock", "pop"), returned=4):
    db = MagicMock()
    select = db.table.return_value.select.return_value.in_.return_value
    select.execute.return_value = SimpleNamespace(data=[{"slug": s} for s in known])
    db.rpc.return_value.execute.return_value = SimpleNamespace(data=returned)
    return db


def test_rpc_params_orders_sort_order_and_position_from_the_lists():
    playlists, tracks = _rpc_params(_request())

    assert [(p["genre_slug"], p["sort_order"]) for p in playlists] == [
        ("rock", 1),
        ("rock", 2),
        ("pop", 1),
    ]
    assert playlists[0]["tracks"] == [
        {"track_id": "t2", "position": 1},
        {"track_id": "t1", "position": 2},
    ]
    assert playlists[0]["description"] == "Desc"
    assert playlists[0]["category"] == "Cat"
    assert [t["track_id"] for t in tracks] == ["t1", "t2"]
    assert tracks[0]["artists"] == [
        {"id": "UC1", "name": "A"},
        {"id": None, "name": "B"},
    ]


def test_rpc_params_passes_missing_description_and_category_as_null():
    playlists, _ = _rpc_params(_request(with_text=False))

    assert playlists[0]["description"] is None
    assert playlists[0]["category"] is None
    assert "description" in playlists[0] and "category" in playlists[0]


def test_rpc_params_is_deterministic():
    assert _rpc_params(_request()) == _rpc_params(_request())


def test_ingest_returns_the_counts_with_one_rpc():
    db = _db()

    result = ingest_genres(db, _request())

    assert (result.genres, result.playlists, result.tracks) == (2, 3, 4)
    db.rpc.assert_called_once()
    assert db.rpc.call_args.args[0] == "replace_genre_playlists"


def test_unknown_slug_raises_not_found_without_rpc_and_logs_only_the_count(caplog):
    db = _db(known=("rock",))

    with caplog.at_level(logging.WARNING), pytest.raises(NotFound) as raised:
        ingest_genres(db, _request())

    assert raised.value.reason == "genre_not_found"
    db.rpc.assert_not_called()
    messages = [r.getMessage() for r in caplog.records]
    assert "genre_ingest_unknown_slug count=1" in messages
    assert all("pop" not in m for m in messages)


@pytest.mark.parametrize("returned", [3, 5, 0, True, None, "4"])
def test_count_mismatch_raises_upstream_error_and_logs_both_numbers(caplog, returned):
    db = _db(returned=returned)

    with caplog.at_level(logging.ERROR), pytest.raises(UpstreamError):
        ingest_genres(db, _request())

    assert any("expected=4" in r.getMessage() for r in caplog.records)


def test_genres_read_failure_is_upstream_error_and_calls_no_rpc():
    db = _db()
    db.table.return_value.select.return_value.in_.return_value.execute.side_effect = (
        APIError({"message": "boom"})
    )

    with pytest.raises(UpstreamError):
        ingest_genres(db, _request())

    db.rpc.assert_not_called()


def test_rpc_failure_is_upstream_error():
    db = _db()
    db.rpc.return_value.execute.side_effect = APIError({"message": "boom"})

    with pytest.raises(UpstreamError):
        ingest_genres(db, _request())


def test_rpc_timeout_is_upstream_timeout():
    db = _db()
    db.rpc.return_value.execute.side_effect = httpx.ReadTimeout("slow")

    with pytest.raises(UpstreamTimeout):
        ingest_genres(db, _request())


# --- Releases ----------------------------------------------------------------


def _release_request(count=3):
    return ReleaseIngestRequest.model_validate(
        {
            "releases": [
                {
                    "album_id": f"MPREb_{n}",
                    "audio_playlist_id": f"OLAK5uy_{n}",
                    "type": "single",
                    "title": f"Release {n}",
                    "artists": [{"id": f"UC{n:022d}", "name": f"Artist {n}"}],
                    "thumbnail_url": f"https://example.com/{n}.jpg",
                }
                for n in range(1, count + 1)
            ]
        }
    )


def _release_db(data=None, error=None):
    db = MagicMock()
    execute = db.rpc.return_value.execute
    if error is not None:
        execute.side_effect = error
    else:
        execute.return_value = SimpleNamespace(data=data)
    return db


def test_release_rpc_params_number_positions_from_the_list_order():
    request = _release_request(3)

    params = _release_rpc_params(request)

    assert [p["position"] for p in params] == [1, 2, 3]
    assert [p["album_id"] for p in params] == ["MPREb_1", "MPREb_2", "MPREb_3"]
    assert set(params[0]) == {
        "album_id",
        "audio_playlist_id",
        "type",
        "title",
        "artists",
        "thumbnail_url",
        "position",
    }
    assert params[0]["artists"] == [{"id": f"UC{1:022d}", "name": "Artist 1"}]
    assert _release_rpc_params(request) == params


def test_ingest_releases_returns_the_count_with_one_rpc():
    db = _release_db(data=3)

    result = ingest_releases(db, _release_request(3))

    assert result.releases == 3
    db.rpc.assert_called_once()
    name, args = db.rpc.call_args.args
    assert name == "replace_new_releases"
    assert len(args["p_releases"]) == 3
    db.table.assert_not_called()


@pytest.mark.parametrize("returned", [2, 0, 4, True, "3", None])
def test_release_count_mismatch_raises_upstream_error_and_logs_both_numbers(
    caplog, returned
):
    db = _release_db(data=returned)

    with caplog.at_level(logging.ERROR), pytest.raises(UpstreamError):
        ingest_releases(db, _release_request(3))

    message = caplog.records[-1].getMessage()
    assert "expected=3" in message
    assert f"returned={returned!r}" in message


def test_release_rpc_failure_is_upstream_error():
    db = _release_db(error=APIError({"message": "boom"}))

    with pytest.raises(UpstreamError):
        ingest_releases(db, _release_request())


def test_release_rpc_timeout_is_upstream_timeout():
    db = _release_db(error=httpx.ReadTimeout("slow"))

    with pytest.raises(UpstreamTimeout):
        ingest_releases(db, _release_request())
