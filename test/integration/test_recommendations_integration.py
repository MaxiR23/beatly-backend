# test/integration/test_recommendations_integration.py
#
# Integration tests of the recommendations endpoints and of migrations 047
# and 048 against the local stack.
#
# Tested:
# - GET /recommendations returns only the caller's rows of the for_you bucket
#   and the empty set for a user with none
# - POST /recommendations/generate stores 30 rows with the real replacing
#   function from five favorites; stores nothing without favorites or plays
# - It replaces every previous row of the user (an expired set and a legacy
#   bucket) and leaves other users' rows alone; a valid set is kept as is
# - POST never recommends an artist played 3 or more times in 90 days, with
#   the top-N turned off so the threshold is what excludes it
# - A failure of the provider keeps the previous set; a seed the provider
#   confirms is gone is skipped and the set is still saved
# - The play history (the top functions over real play_events) picks the
#   listened artists and album seeds; the tier counts the plays after the
#   previous generated_at
# - get_top_played_artists groups by the primary artist inside the window
#   and keeps only the artists with at least p_min_plays plays (inclusive,
#   per user, inside the window),
#   get_top_played_albums skips the plays without album_id, count_plays_since
#   includes the boundary
# - get_top_played_artists exists only with p_min_plays (048 dropped the
#   three-argument form of 047): a call without it is PGRST202
# - replace_user_recommendations rolls the DELETE back when the INSERT fails
#   and can be called twice with the same items
# - anon and authenticated can neither touch the table nor call the four
#   functions (42501)
#
# What is covered:
# - The queries, the four functions and the privileges of migrations 047
#   and 048 against the real schema. The provider is replaced at the module
#   level of the service (get_artist, get_album and the probe): nothing goes
#   out to it
#
# Run with: pytest -m integration test/integration/test_recommendations_integration.py -v
#
# SEE: routes/recommendations.py, services/recommendations_service.py,
#      db/migrations/047_user_recommendations_backend_only.sql,
#      db/migrations/048_get_top_played_artists_min_plays.sql

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from postgrest.exceptions import APIError
from supabase import create_client

from core.config import settings
from core.database import get_user_client
from core.exceptions import UpstreamError
from core.search_provider import ProviderResourceMissing
from models.album import Album, AlbumRef
from models.artist import Artist, ArtistRef
from services import recommendations_service

pytestmark = pytest.mark.integration

# Fixed dates, never the host clock: the Docker clock can drift from it.
_GENERATED_AT = "2001-01-01T00:00:00+00:00"
_EXPIRED = "2001-01-31T00:00:00+00:00"
_VALID = "2999-01-01T00:00:00+00:00"
_THUMB = "https://lh3.googleusercontent.com/it=w60-h60-l90-rj"

_SEEDS = [f"UCseed{i}" for i in range(1, 6)]


def _related(i):
    return [f"UCrel{i}x{j}" for j in range(1, 5)]


def _albums(similar_id):
    return [f"MPREb_{similar_id}_{k}" for k in range(1, 4)]


def _artist(artist_id, related=(), albums=()):
    return Artist(
        id=artist_id,
        name=f"Name {artist_id}",
        thumbnail_url=_THUMB,
        related=[
            ArtistRef(id=r, name=f"Name {r}", thumbnail_url=_THUMB) for r in related
        ],
        albums=[
            AlbumRef(id=a, title=f"Title {a}", thumbnail_url=_THUMB) for a in albums
        ],
    )


def _world(seeds=_SEEDS):
    pages = {}
    for i, seed in enumerate(seeds, start=1):
        pages[seed] = _artist(seed, related=_related(i))
        for similar in _related(i):
            pages[similar] = _artist(similar, albums=_albums(similar))
    return pages


def _install(monkeypatch, pages, *, albums=None, failing=(), gone=()):
    # The provider never answers: get_artist/get_album are replaced where
    # the service looks them up. `gone` ids raise the failure of a missing
    # page and the probe confirms it; `failing` ids raise a plain failure.
    # Attribute patches, not dependency overrides: the client fixture
    # requires app.dependency_overrides to be empty.
    def get_artist(provider, cache, artist_id):
        if artist_id in failing:
            raise UpstreamError()
        if artist_id in gone:
            raise _missing()
        return pages[artist_id], 1

    def get_album(provider, cache, album_id):
        if album_id in gone:
            raise _missing()
        return (albums or {})[album_id], 1

    def probe(provider, browse_id, cause):
        if browse_id in gone:
            raise ProviderResourceMissing(browse_id) from cause
        raise AssertionError(f"probed an id that is not gone: {browse_id}")

    monkeypatch.setattr(recommendations_service, "get_artist", get_artist)
    monkeypatch.setattr(recommendations_service, "get_album", get_album)
    monkeypatch.setattr(
        recommendations_service, "provider_raise_if_browse_missing", probe
    )


def _missing():
    error = UpstreamError()
    error.__cause__ = KeyError("contents")
    return error


def _fail_if_called(monkeypatch):
    def refuse(*_args):
        raise AssertionError("the provider must not be called")

    monkeypatch.setattr(recommendations_service, "get_artist", refuse)
    monkeypatch.setattr(recommendations_service, "get_album", refuse)


def _favorite(admin, user, external_id):
    admin.table("library_items").insert(
        {
            "user_id": user.user_id,
            "kind": "artist",
            "external_id": external_id,
            "title": external_id,
            "source": "external",
        }
    ).execute()


def _favorites(admin, user, ids=_SEEDS):
    for external_id in ids:
        _favorite(admin, user, external_id)


def _rows(admin, user, bucket=None):
    query = admin.table("user_recommendations").select("*").eq("user_id", user.user_id)
    if bucket is not None:
        query = query.eq("bucket", bucket)
    return query.execute().data


def _seed_set(
    admin, user, *, bucket="for_you", generated_at=_GENERATED_AT, valid=_EXPIRED
):
    admin.table("user_recommendations").insert(
        [
            {
                "user_id": user.user_id,
                "bucket": bucket,
                "item_type": item_type,
                "item_id": f"old-{item_type}-{rank}",
                "rank": rank,
                "seed_type": "artist",
                "seed_id": "old-seed",
                "seed_name": "Old seed",
                "payload": (
                    {"name": "Old", "thumbnail_url": _THUMB}
                    if item_type == "artist"
                    else {"title": "Old album", "thumbnail_url": _THUMB}
                ),
                "source": "similar_v1",
                "generated_at": generated_at,
                "valid_until": valid,
            }
            for item_type in ("artist", "album")
            for rank in (1, 2)
        ]
    ).execute()


def _play(admin, user, artist_id, album_id, played_at):
    metadata = {"artists": [{"id": artist_id, "name": "A"}]}
    if album_id is not None:
        metadata["album_id"] = album_id
    admin.table("play_events").insert(
        {
            "user_id": user.user_id,
            "track_id": f"it-{uuid4().hex}",
            "played_at": played_at.isoformat(),
            "metadata": metadata,
        }
    ).execute()


def _generate(client, user):
    return client.post("/recommendations/generate", headers=user.headers)


# --- GET ---------------------------------------------------------------------


def test_get_recommendations_returns_only_the_callers_set(client, make_user, admin):
    user = make_user()
    other = make_user()
    _seed_set(admin, user, valid=_VALID)
    _seed_set(admin, user, bucket="2026-W01", valid=_VALID)
    _seed_set(admin, other, valid=_VALID)

    response = client.get("/recommendations", headers=user.headers)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert [a["id"] for a in data["artists"]] == ["old-artist-1", "old-artist-2"]
    assert [a["id"] for a in data["albums"]] == ["old-album-1", "old-album-2"]
    assert data["valid_until"] == "2999-01-01T00:00:00+00:00"
    assert data["generated_at"] == _GENERATED_AT


def test_get_recommendations_of_a_new_user_is_the_empty_set(client, make_user):
    user = make_user()

    response = client.get("/recommendations", headers=user.headers)

    assert response.status_code == 200
    assert response.json()["data"] == {
        "artists": [],
        "albums": [],
        "generated_at": None,
        "valid_until": None,
    }


# --- POST --------------------------------------------------------------------


def test_generate_with_five_favorites_stores_thirty_rows(
    client, make_user, admin, monkeypatch
):
    user = make_user()
    _favorites(admin, user)
    _install(monkeypatch, _world())

    response = _generate(client, user)

    assert response.status_code == 200, response.text
    rows = _rows(admin, user)
    assert len(rows) == 30
    assert {r["bucket"] for r in rows} == {"for_you"}
    assert {r["source"] for r in rows} == {"similar_v1"}
    assert len({(r["item_type"], r["item_id"]) for r in rows}) == 30
    data = response.json()["data"]
    assert len(data["artists"]) == 15
    assert len(data["albums"]) == 15
    generated = datetime.fromisoformat(data["generated_at"])
    assert datetime.fromisoformat(data["valid_until"]) - generated == timedelta(days=30)
    assert client.get("/recommendations", headers=user.headers).json()["data"] == data


def test_generate_for_a_user_without_favorites_or_plays_writes_nothing(
    client, make_user, admin, monkeypatch
):
    user = make_user()
    _fail_if_called(monkeypatch)

    response = _generate(client, user)

    assert response.status_code == 200
    assert response.json()["data"]["artists"] == []
    assert _rows(admin, user) == []


def test_generate_with_a_valid_set_keeps_the_rows(
    client, make_user, admin, monkeypatch
):
    user = make_user()
    _favorites(admin, user)
    _seed_set(admin, user, valid=_VALID)
    before = _rows(admin, user)
    _fail_if_called(monkeypatch)

    response = _generate(client, user)

    assert response.status_code == 200
    assert response.json()["data"]["generated_at"] == _GENERATED_AT
    assert sorted(_rows(admin, user), key=lambda r: r["id"]) == sorted(
        before, key=lambda r: r["id"]
    )


def test_generate_replaces_every_previous_row_of_the_user(
    client, make_user, admin, monkeypatch
):
    user = make_user()
    other = make_user()
    _favorites(admin, user)
    _seed_set(admin, user)
    _seed_set(admin, user, bucket="2026-W01")
    _seed_set(admin, other, valid=_VALID)
    _install(monkeypatch, _world())

    response = _generate(client, user)

    assert response.status_code == 200, response.text
    rows = _rows(admin, user)
    assert len(rows) == 30
    assert {r["bucket"] for r in rows} == {"for_you"}
    assert not [r for r in rows if r["item_id"].startswith("old-")]
    assert len(_rows(admin, other)) == 4


def test_generate_provider_failure_keeps_the_previous_set(
    client, make_user, admin, monkeypatch
):
    user = make_user()
    _favorites(admin, user)
    _seed_set(admin, user)
    before = sorted(_rows(admin, user), key=lambda r: r["id"])
    _install(monkeypatch, _world(), failing={"UCrel2x1"})

    response = _generate(client, user)

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
    assert sorted(_rows(admin, user), key=lambda r: r["id"]) == before


def test_generate_with_one_missing_seed_saves_the_set(
    client, make_user, admin, monkeypatch
):
    user = make_user()
    _favorites(admin, user)
    _install(monkeypatch, _world(), gone={"UCseed3"})

    response = _generate(client, user)

    assert response.status_code == 200, response.text
    rows = _rows(admin, user)
    assert len(rows) == 30
    assert "UCseed3" not in {r["seed_id"] for r in rows}


def test_generate_with_plays_uses_the_play_history(
    client, make_user, admin, monkeypatch
):
    user = make_user()
    recent = datetime.now(UTC) - timedelta(days=1)
    for _ in range(3):
        _play(admin, user, "UCplayed1", "MPREbplayed", recent)
    _play(admin, user, "UCplayed2", None, recent)
    pages = _world(seeds=["UCplayed1", "UCplayed2"])
    albums = {
        "MPREbplayed": Album(
            id="MPREbplayed",
            title="Played album",
            duration_seconds=1,
            tracks=[],
            related_recommendations=[
                AlbumRef(id=f"MPREbrec{k}", title=f"Rec {k}") for k in range(1, 4)
            ],
        )
    }
    _install(monkeypatch, pages, albums=albums)

    response = _generate(client, user)

    assert response.status_code == 200, response.text
    rows = _rows(admin, user)
    seeds = {(r["seed_type"], r["seed_id"]) for r in rows}
    assert ("artist", "UCplayed1") in seeds
    assert ("artist", "UCplayed2") in seeds
    assert ("album", "MPREbplayed") in seeds


def test_generate_excludes_artists_played_three_or_more_times(
    client, make_user, admin, monkeypatch
):
    user = make_user()
    _favorites(admin, user)
    # 60 days ago: outside the 30-day seed window (so they are not seeds)
    # and inside the 90-day exclusion window.
    played_at = datetime.now(UTC) - timedelta(days=60)
    for _ in range(3):
        _play(admin, user, "UCrel1x1", None, played_at)
    for _ in range(2):
        _play(admin, user, "UCrel1x2", None, played_at)
    # With the real database the top 20 would exclude both: this test has to
    # prove the threshold alone.
    monkeypatch.setattr(settings, "recommendations_exclude_top_artists", 0)
    _install(monkeypatch, _world())

    response = _generate(client, user)

    assert response.status_code == 200, response.text
    stored = [r for r in _rows(admin, user) if r["item_type"] == "artist"]
    ids = {r["item_id"] for r in stored}
    assert "UCrel1x1" not in ids
    assert "UCrel1x2" in ids
    assert next(r for r in stored if r["item_id"] == "UCrel1x2")["seed_id"] == (
        "UCseed1"
    )
    assert len(_rows(admin, user)) == 30


def test_generate_tier_counts_plays_after_the_previous_generated_at(
    client, make_user, admin, monkeypatch
):
    user = make_user()
    _favorites(admin, user, ids=["UCseed1"])
    _seed_set(admin, user)
    for day in range(20):
        _play(
            admin,
            user,
            "UCx",
            None,
            datetime(2001, 6, 1, tzinfo=UTC) + timedelta(days=day),
        )
    for _ in range(3):
        _play(admin, user, "UCx", None, datetime(2000, 6, 1, tzinfo=UTC))
    _install(monkeypatch, _world(seeds=["UCseed1"]))

    response = _generate(client, user)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    generated = datetime.fromisoformat(data["generated_at"])
    assert datetime.fromisoformat(data["valid_until"]) - generated == timedelta(days=7)


# --- The functions of migrations 047 and 048 ---------------------------------


def test_top_played_artists_groups_by_primary_artist_within_the_window(
    make_user, admin
):
    user = make_user()
    other = make_user()
    day = datetime(2005, 6, 1, tzinfo=UTC)
    for _ in range(3):
        _play(admin, user, "UCa", None, day)
    for _ in range(2):
        _play(admin, user, "UCb", None, day + timedelta(days=1))
    _play(admin, user, "UCc", None, day + timedelta(days=2))
    _play(admin, user, "UCold", None, datetime(2001, 1, 1, tzinfo=UTC))
    admin.table("play_events").insert(
        {
            "user_id": user.user_id,
            "track_id": f"it-{uuid4().hex}",
            "played_at": day.isoformat(),
            "metadata": {"artists": [{"id": ""}]},
        }
    ).execute()
    _play(admin, other, "UCother", None, day)

    result = admin.rpc(
        "get_top_played_artists",
        {
            "p_user_id": user.user_id,
            "p_since": datetime(2005, 1, 1, tzinfo=UTC).isoformat(),
            "p_min_plays": 1,
            "p_limit": 2,
        },
    ).execute()

    assert result.data == [
        {"artist_id": "UCa", "play_count": 3},
        {"artist_id": "UCb", "play_count": 2},
    ]


def test_top_played_artists_keeps_only_artists_with_at_least_min_plays(
    make_user, admin
):
    user = make_user()
    other = make_user()
    day = datetime(2005, 6, 1, tzinfo=UTC)
    for artist, times in (("UCa", 4), ("UCb", 3), ("UCc", 2)):
        for _ in range(times):
            _play(admin, user, artist, None, day)
    for _ in range(3):
        _play(admin, user, "UCold", None, datetime(2001, 1, 1, tzinfo=UTC))
        _play(admin, other, "UCother", None, day)

    result = admin.rpc(
        "get_top_played_artists",
        {
            "p_user_id": user.user_id,
            "p_since": datetime(2005, 1, 1, tzinfo=UTC).isoformat(),
            "p_min_plays": 3,
            "p_limit": 10,
        },
    ).execute()

    assert result.data == [
        {"artist_id": "UCa", "play_count": 4},
        {"artist_id": "UCb", "play_count": 3},
    ]


def test_top_played_artists_has_no_three_argument_form(make_user, admin):
    user = make_user()

    with pytest.raises(APIError) as raised:
        admin.rpc(
            "get_top_played_artists",
            {"p_user_id": user.user_id, "p_since": _GENERATED_AT, "p_limit": 1},
        ).execute()

    assert raised.value.code == "PGRST202"


def test_top_played_albums_skips_plays_without_album_id(make_user, admin):
    user = make_user()
    day = datetime(2005, 6, 1, tzinfo=UTC)
    _play(admin, user, "UCa", "MPREb1", day)
    _play(admin, user, "UCa", "MPREb1", day)
    _play(admin, user, "UCa", "MPREb2", day)
    _play(admin, user, "UCa", None, day)

    result = admin.rpc(
        "get_top_played_albums",
        {
            "p_user_id": user.user_id,
            "p_since": datetime(2005, 1, 1, tzinfo=UTC).isoformat(),
            "p_limit": 10,
        },
    ).execute()

    assert result.data == [
        {"album_id": "MPREb1", "play_count": 2},
        {"album_id": "MPREb2", "play_count": 1},
    ]


def test_count_plays_since_includes_the_boundary(make_user, admin):
    user = make_user()
    boundary = datetime(2005, 6, 1, tzinfo=UTC)
    _play(admin, user, "UCa", None, boundary - timedelta(seconds=1))
    _play(admin, user, "UCa", None, boundary)
    _play(admin, user, "UCa", None, boundary + timedelta(seconds=1))

    result = admin.rpc(
        "count_plays_since",
        {"p_user_id": user.user_id, "p_since": boundary.isoformat()},
    ).execute()

    assert result.data == 2


def _replace(admin, user, rows):
    return admin.rpc(
        "replace_user_recommendations",
        {
            "p_user_id": user.user_id,
            "p_bucket": "for_you",
            "p_source": "similar_v1",
            "p_generated_at": _GENERATED_AT,
            "p_valid_until": _EXPIRED,
            "p_rows": rows,
        },
    ).execute()


def _new_row(item_id, rank):
    return {
        "item_type": "artist",
        "item_id": item_id,
        "rank": rank,
        "seed_type": "artist",
        "seed_id": "UCseed",
        "seed_name": "Seed",
        "seed_thumb": None,
        "payload": {"name": item_id, "thumbnail_url": None},
    }


def test_replace_rpc_failure_keeps_the_previous_set(make_user, admin):
    user = make_user()
    _seed_set(admin, user)
    before = sorted(_rows(admin, user), key=lambda r: r["id"])

    with pytest.raises(APIError) as raised:
        _replace(admin, user, [_new_row("dup", 1), _new_row("dup", 2)])

    assert raised.value.code == "23505"
    assert sorted(_rows(admin, user), key=lambda r: r["id"]) == before


def test_replace_rpc_twice_with_the_same_items_succeeds(make_user, admin):
    user = make_user()
    rows = [_new_row("a", 1), _new_row("b", 2)]

    first = _replace(admin, user, rows)
    second = _replace(admin, user, rows)

    assert first.data == 2
    assert second.data == 2
    stored = _rows(admin, user)
    assert sorted(r["item_id"] for r in stored) == ["a", "b"]
    assert {r["payload"]["name"] for r in stored} == {"a", "b"}


def test_anon_and_authenticated_cannot_touch_the_table_or_call_the_functions(
    make_user,
):
    user = make_user()
    clients = [
        create_client(settings.supabase_url, settings.supabase_anon_key),
        get_user_client(make_user().token),
    ]
    since = _GENERATED_AT
    calls = [
        (
            "get_top_played_artists",
            {
                "p_user_id": user.user_id,
                "p_since": since,
                "p_min_plays": 1,
                "p_limit": 1,
            },
        ),
        (
            "get_top_played_albums",
            {"p_user_id": user.user_id, "p_since": since, "p_limit": 1},
        ),
        ("count_plays_since", {"p_user_id": user.user_id, "p_since": since}),
        (
            "replace_user_recommendations",
            {
                "p_user_id": user.user_id,
                "p_bucket": "for_you",
                "p_source": "similar_v1",
                "p_generated_at": since,
                "p_valid_until": since,
                "p_rows": [],
            },
        ),
    ]

    for client in clients:
        with pytest.raises(APIError) as selected:
            client.table("user_recommendations").select("*").execute()
        with pytest.raises(APIError) as inserted:
            client.table("user_recommendations").insert(
                {**_new_row("x", 1), "user_id": user.user_id, "bucket": "for_you"}
            ).execute()
        assert selected.value.code == "42501"
        assert inserted.value.code == "42501"
        for name, params in calls:
            with pytest.raises(APIError) as raised:
                client.rpc(name, params).execute()
            assert raised.value.code == "42501", name
