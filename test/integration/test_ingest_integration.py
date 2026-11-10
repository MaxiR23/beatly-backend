# test/integration/test_ingest_integration.py
#
# Integration tests of POST /ingest/genres and of replace_genre_playlists
# (migration 052) against the local stack.
#
# Tested:
# - A valid submission answers 200 with the counts and is served by
#   GET /genres/{slug}/playlists (in sort_order) and
#   GET /genre-playlists/{id}/tracks (tracks in order, position 1..n, the
#   first artist with an id)
# - track_count is the number of saved rows and thumbnail_url the cover of
#   the first saved track that has one (a track with an empty cover is skipped)
# - Sending the same body twice gives the same response, the same playlist
#   ids, the same tracks in the same positions and the same counts
# - A second ingest of a source playlist keeps its id and replaces its tracks
# - The historical playlists (no source_playlist_id) and the playlists that
#   are not in the body are deleted for the genres of the body; the genres
#   that are not in the body are untouched
# - The metadata of a track that already exists is overwritten
# - A slug that does not exist is 404 genre_not_found and writes nothing, not
#   even the tracks; a body that is rejected (422) or has no token (401)
#   writes nothing; with INGEST_TOKEN unset everything is 401
# - The function is atomic: an unknown slug (P0002), a repeated position
#   (23505), a primary artist without id (23514) and a playlist with no
#   tracks (22023) fail and keep the previous playlists and tracks
# - Two ingests at the same time on the same genre both finish and the final
#   state is exactly one of the two submissions
# - anon and authenticated cannot execute the function (42501)
#
# What is covered:
# - The queries and the RPC of the route and the migration 052 against the
#   real schema. The overlap of two ingests cannot be forced from PostgREST:
#   the test proves the result is right and that none fails
#
# Run with: pytest -m integration test/integration/test_ingest_integration.py -v
#
# SEE: routes/ingest.py, services/ingest_service.py, db/migrations/052_replace_genre_playlists.sql

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from postgrest.exceptions import APIError
from pydantic import SecretStr
from supabase import create_client

from core.config import settings
from core.database import get_ingest_db, get_user_client

pytestmark = pytest.mark.integration

_TOKEN = "a-fake-ingest-token-made-in-the-test-0123456789"
_COVER = "https://example.com/cover-{}.jpg"


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(settings, "ingest_token", SecretStr(_TOKEN))


def _genre(admin):
    suffix = uuid4().hex[:12]
    row = (
        admin.table("genres")
        .insert({"name": f"Genre {suffix}", "slug": f"it-{suffix}"})
        .execute()
        .data[0]
    )
    return row


def _track(track_id, **overrides):
    return {
        "track_id": track_id,
        "title": f"Title {track_id}",
        "artists": [{"id": "UCartist", "name": "Artist"}, {"id": None, "name": "G"}],
        "album": "Album",
        "album_id": "MPREb_album",
        "duration_seconds": 200,
        "thumbnail_url": _COVER.format(track_id),
        **overrides,
    }


def _ids(count):
    tag = uuid4().hex[:10]
    return [f"it-{tag}-{i}" for i in range(count)]


def _body(genres, tracks=None):
    # genres: {slug: [(source_playlist_id, title, [track ids])]}
    out = []
    ids: list[str] = []
    for slug, playlists in genres.items():
        out.append(
            {
                "slug": slug,
                "playlists": [
                    {
                        "source_playlist_id": source,
                        "title": title,
                        "description": None,
                        "category": None,
                        "tracks": list(track_ids),
                    }
                    for source, title, track_ids in playlists
                ],
            }
        )
        for _, _, track_ids in playlists:
            ids.extend(i for i in track_ids if i not in ids)
    rows = tracks if tracks is not None else [_track(i) for i in ids]
    return {"genres": out, "tracks": rows}


def _post(client, body, token=_TOKEN):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/ingest/genres", json=body, headers=headers)


def _state(admin, genre_id):
    playlists = (
        admin.table("genre_playlists")
        .select("*")
        .eq("genre_id", genre_id)
        .order("sort_order")
        .execute()
        .data
    )
    state = []
    for playlist in playlists:
        linked = (
            admin.table("genre_playlist_tracks")
            .select("track_id, position")
            .eq("playlist_id", playlist["id"])
            .order("position")
            .execute()
            .data
        )
        state.append(
            {
                "id": playlist["id"],
                "source": playlist["source_playlist_id"],
                "title": playlist["title"],
                "sort_order": playlist["sort_order"],
                "track_count": playlist["track_count"],
                "thumbnail_url": playlist["thumbnail_url"],
                "tracks": [(r["position"], r["track_id"]) for r in linked],
            }
        )
    return state


def _stored(admin, track_ids):
    return (
        admin.table("tracks").select("track_id").in_("track_id", track_ids).execute()
    ).data


def _rpc(db, playlists, tracks):
    return db.rpc(
        "replace_genre_playlists", {"p_playlists": playlists, "p_tracks": tracks}
    ).execute()


def _rpc_playlist(slug, source, track_ids, title="T", sort_order=1, positions=None):
    positions = positions or range(1, len(track_ids) + 1)
    return {
        "genre_slug": slug,
        "source_playlist_id": source,
        "title": title,
        "description": None,
        "category": None,
        "sort_order": sort_order,
        "tracks": [
            {"track_id": t, "position": p} for t, p in zip(track_ids, positions)
        ],
    }


# --- Served by the genre endpoints -------------------------------------------


def test_valid_ingest_is_served_by_the_genre_endpoints(client, make_user, admin):
    genre = _genre(admin)
    a, b, c = _ids(3)
    body = _body(
        {
            genre["slug"]: [
                ("PLfirst", "First", [a, b]),
                ("PLsecond", "Second", [c, a]),
            ]
        }
    )

    response = _post(client, body)

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "data": {"genres": 1, "playlists": 2, "tracks": 4},
    }
    user = make_user()
    listed = client.get(f"/genres/{genre['slug']}/playlists", headers=user.headers)
    assert listed.status_code == 200
    items = listed.json()["data"]["items"]
    assert [i["title"] for i in items] == ["First", "Second"]
    assert [i["track_count"] for i in items] == [2, 2]
    tracks = client.get(
        f"/genre-playlists/{items[1]['id']}/tracks", headers=user.headers
    )
    assert tracks.status_code == 200
    rows = tracks.json()["data"]["items"]
    assert [(r["position"], r["track_id"]) for r in rows] == [(1, c), (2, a)]
    assert rows[0]["artists"][0]["id"] == "UCartist"
    assert rows[0]["album_id"] == "MPREb_album"


def test_track_count_and_thumbnail_come_from_the_saved_tracks(admin):
    genre = _genre(admin)
    a, b = _ids(2)
    # The endpoint requires https; the function still has to skip an empty one.
    tracks = [_track(a, thumbnail_url=""), _track(b)]

    written = _rpc(
        admin, [_rpc_playlist(genre["slug"], "PLcover", [a, b])], tracks
    ).data

    assert written == 2
    [playlist] = _state(admin, genre["id"])
    assert playlist["track_count"] == 2
    assert playlist["thumbnail_url"] == _COVER.format(b)


# --- Identity and scope ------------------------------------------------------


def test_resending_the_same_body_gives_the_same_result(client, admin):
    genre = _genre(admin)
    ids = _ids(4)
    body = _body(
        {genre["slug"]: [("PLa", "A", ids[:3]), ("PLb", "B", [ids[3], ids[0]])]}
    )

    first = _post(client, body)
    state_first = _state(admin, genre["id"])
    second = _post(client, body)
    state_second = _state(admin, genre["id"])

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert state_first == state_second
    assert [p["track_count"] for p in state_second] == [3, 2]


def test_second_ingest_keeps_the_playlist_id_and_replaces_its_tracks(client, admin):
    genre = _genre(admin)
    a, b, c = _ids(3)
    assert (
        _post(client, _body({genre["slug"]: [("PLa", "Old", [a, b])]})).status_code
        == 200
    )
    [before] = _state(admin, genre["id"])

    response = _post(client, _body({genre["slug"]: [("PLa", "New", [c, a])]}))

    assert response.status_code == 200
    [after] = _state(admin, genre["id"])
    assert after["id"] == before["id"]
    assert after["title"] == "New"
    assert after["tracks"] == [(1, c), (2, a)]
    assert after["track_count"] == 2


def test_historical_and_absent_playlists_of_a_genre_in_scope_are_deleted(
    client, admin, seed_genre_playlist
):
    seeded = seed_genre_playlist()
    slug = seeded["genre"]["slug"]
    genre_id = seeded["genre"]["id"]
    a, b, c = _ids(3)
    assert (
        _post(client, _body({slug: [("PLa", "A", [a]), ("PLb", "B", [b])]})).status_code
        == 200
    )
    assert {p["source"] for p in _state(admin, genre_id)} == {"PLa", "PLb"}
    kept = {p["source"]: p["id"] for p in _state(admin, genre_id)}

    response = _post(client, _body({slug: [("PLa", "A", [a, c])]}))

    assert response.status_code == 200
    state = _state(admin, genre_id)
    assert [p["source"] for p in state] == ["PLa"]
    assert state[0]["id"] == kept["PLa"]
    # The historical playlist is gone with its rows.
    assert (
        admin.table("genre_playlists")
        .select("id")
        .eq("id", seeded["playlist"]["id"])
        .execute()
        .data
        == []
    )
    assert (
        admin.table("genre_playlist_tracks")
        .select("id")
        .eq("playlist_id", seeded["playlist"]["id"])
        .execute()
        .data
        == []
    )
    assert (
        admin.table("genre_playlists").select("id").eq("id", kept["PLb"]).execute().data
        == []
    )


def test_genres_absent_from_the_body_are_untouched(client, admin, seed_genre_playlist):
    other = seed_genre_playlist()
    before = _state(admin, other["genre"]["id"])
    genre = _genre(admin)

    response = _post(client, _body({genre["slug"]: [("PLa", "A", _ids(2))]}))

    assert response.status_code == 200
    assert _state(admin, other["genre"]["id"]) == before
    assert before != []


def test_existing_track_metadata_is_overwritten(client, admin, seed_tracks):
    [existing] = seed_tracks(1)
    genre = _genre(admin)
    track = _track(
        existing["track_id"],
        title="New title",
        album="New album",
        album_id="MPREb_new",
        duration_seconds=321,
        thumbnail_url="https://example.com/new.jpg",
        artists=[{"id": "UCnew", "name": "New artist"}],
    )

    response = _post(
        client,
        _body({genre["slug"]: [("PLa", "A", [existing["track_id"]])]}, [track]),
    )

    assert response.status_code == 200
    row = (
        admin.table("tracks")
        .select("title, artists, album, album_id, duration_seconds, thumbnail_url")
        .eq("track_id", existing["track_id"])
        .execute()
        .data[0]
    )
    assert row == {
        "title": "New title",
        "artists": [{"id": "UCnew", "name": "New artist"}],
        "album": "New album",
        "album_id": "MPREb_new",
        "duration_seconds": 321,
        "thumbnail_url": "https://example.com/new.jpg",
    }


# --- Rejections write nothing ------------------------------------------------


def test_unknown_slug_is_404_and_writes_nothing(client, admin):
    genre = _genre(admin)
    a, b = _ids(2)
    assert _post(client, _body({genre["slug"]: [("PLa", "A", [a])]})).status_code == 200
    before = _state(admin, genre["id"])
    new_id = _ids(1)[0]

    response = _post(
        client,
        _body(
            {
                genre["slug"]: [("PLa", "Changed", [b])],
                f"it-missing-{uuid4().hex[:8]}": [("PLz", "Z", [new_id])],
            }
        ),
    )

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "genre_not_found"}
    assert _state(admin, genre["id"]) == before
    assert _stored(admin, [b, new_id]) == []


def test_rejected_body_writes_nothing(client, admin):
    genre = _genre(admin)
    a, b = _ids(2)
    body = _body({genre["slug"]: [("PLa", "A", [a])]})
    orphan = {**body, "tracks": [*body["tracks"], _track(b)]}

    invalid = _post(client, orphan)
    no_token = _post(client, body, token=None)
    wrong_token = _post(client, body, token="wrong")

    assert invalid.status_code == 422
    assert invalid.json() == {"ok": False, "reason": "invalid_request"}
    assert no_token.status_code == wrong_token.status_code == 401
    assert _state(admin, genre["id"]) == []
    assert _stored(admin, [a, b]) == []


def test_unset_token_is_401(client, admin, monkeypatch):
    monkeypatch.setattr(settings, "ingest_token", None)
    genre = _genre(admin)
    body = _body({genre["slug"]: [("PLa", "A", _ids(1))]})

    for token in (_TOKEN, None):
        response = _post(client, body, token=token)
        assert response.status_code == 401
        assert response.json() == {"ok": False, "reason": "unauthorized"}
    assert _state(admin, genre["id"]) == []


# --- The function is atomic --------------------------------------------------


def _seed_previous(client, admin):
    genre = _genre(admin)
    a, b = _ids(2)
    assert (
        _post(client, _body({genre["slug"]: [("PLold", "Old", [a, b])]})).status_code
        == 200
    )
    return genre, [a, b], _state(admin, genre["id"])


def test_rpc_unknown_slug_fails_and_keeps_previous_rows(client, admin):
    genre, _, before = _seed_previous(client, admin)
    new = _ids(1)[0]

    with pytest.raises(APIError) as raised:
        _rpc(
            admin,
            [
                _rpc_playlist(genre["slug"], "PLnew", [new]),
                _rpc_playlist("it-no-such-genre", "PLx", [new]),
            ],
            [_track(new)],
        )

    assert raised.value.code == "P0002"
    assert _state(admin, genre["id"]) == before
    assert _stored(admin, [new]) == []


def test_rpc_repeated_position_fails_and_keeps_previous_rows(client, admin):
    genre, _, before = _seed_previous(client, admin)
    x, y = _ids(2)

    with pytest.raises(APIError) as raised:
        _rpc(
            admin,
            [_rpc_playlist(genre["slug"], "PLnew", [x, y], positions=[1, 1])],
            [_track(x), _track(y)],
        )

    assert raised.value.code == "23505"
    assert _state(admin, genre["id"]) == before
    assert _stored(admin, [x, y]) == []


def test_rpc_primary_artist_without_id_fails_and_keeps_previous_rows(client, admin):
    genre, _, before = _seed_previous(client, admin)
    x = _ids(1)[0]

    with pytest.raises(APIError) as raised:
        _rpc(
            admin,
            [_rpc_playlist(genre["slug"], "PLnew", [x])],
            [_track(x, artists=[{"id": None, "name": "No id"}])],
        )

    assert raised.value.code == "23514"
    assert _state(admin, genre["id"]) == before


def test_rpc_rejects_a_playlist_without_tracks_and_keeps_previous_rows(client, admin):
    genre, _, before = _seed_previous(client, admin)
    x = _ids(1)[0]

    with pytest.raises(APIError) as raised:
        _rpc(
            admin,
            [
                _rpc_playlist(genre["slug"], "PLnew", [x]),
                _rpc_playlist(genre["slug"], "PLempty", [], sort_order=2),
            ],
            [_track(x)],
        )
    with pytest.raises(APIError) as empty:
        _rpc(admin, [], [])

    assert raised.value.code == "22023"
    assert empty.value.code == "22023"
    assert _state(admin, genre["id"]) == before
    assert _stored(admin, [x]) == []


def test_two_concurrent_ingests_are_serialized(admin):
    genre = _genre(admin)
    first_ids, second_ids = _ids(200), _ids(200)
    first = (
        [_rpc_playlist(genre["slug"], "PLsame", first_ids, title="first")],
        [_track(i) for i in first_ids],
    )
    second = (
        [_rpc_playlist(genre["slug"], "PLsame", second_ids, title="second")],
        [_track(i) for i in second_ids],
    )
    clients = [admin, get_ingest_db()]

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_rpc, db, *args) for db, args in zip(clients, (first, second))
        ]
        results = [f.result() for f in futures]

    assert [r.data for r in results] == [200, 200]
    [playlist] = _state(admin, genre["id"])
    assert playlist["title"] in ("first", "second")
    expected = first_ids if playlist["title"] == "first" else second_ids
    assert [t for _, t in playlist["tracks"]] == expected
    assert playlist["track_count"] == 200


# --- Privileges --------------------------------------------------------------


def test_anon_and_authenticated_cannot_execute_the_function(make_user):
    clients = [
        create_client(settings.supabase_url, settings.supabase_anon_key),
        get_user_client(make_user().token),
    ]

    for db in clients:
        with pytest.raises(APIError) as called:
            _rpc(db, [], [])
        assert called.value.code == "42501"
