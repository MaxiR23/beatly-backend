# test/services/test_recommendations_service.py
#
# Tests for the pure parts of the recommendations service.
#
# Tested:
# - The validity days follow the tiers at 4, 5, 19 and 20 plays, and the
#   settings when they are not the defaults
# - The artist seeds interleave favorites and listened artists without
#   repeats, skip ids the artist route would reject and stop at five
# - The rounds give each source its next item in turn, never fill from one
#   source while the others still have candidates, skip taken ids and stop
#   at the limit or when a round adds nothing
# - The base title of an album drops every trailing bracketed suffix, and a
#   title is a soundtrack when "Soundtrack" or "OST" is a whole word
# - The rounds skip an item when any of its keys is taken and record all of
#   the keys of what they pick
# - The rows keep one edition of an album (same artist, same base title,
#   across sources and with or without linked artists), leave soundtracks
#   out and fill their slots, and never recommend another edition of an
#   album seed
# - The ranked albums never reserve more history albums than the list holds
# - The rows give an album to the first list that asks for it and never
#   repeat an artist or album id, with the section and rank of each row
# - The chosen artists are cut to what fits the sections and the recommended
#   artists, before their pages are fetched
# - A stored seed row without section index is an upstream error
# - _parallel returns the results in the input order, runs at most the
#   configured workers at once, and does not start the pending calls after
#   a failure
# - A worker runs with the image color scope detached
#
# What is covered:
# - services/recommendations_service.py helpers that need no database
#   or provider; the endpoints are in test/routes/test_recommendations.py
#
# Run with: pytest test/services/test_recommendations_service.py -v
#
# SEE: services/recommendations_service.py

import threading
import time
from types import SimpleNamespace

import pytest

from core.config import settings
from core.image_color_scope import observe_image, open_image_color_scope
from models.album import Album, AlbumRef
from models.artist import Artist, ArtistRef
from models.artist_refs import AlbumArtistRef
from models.recommendations import RecommendationSeed
from services import recommendations_service as service


@pytest.mark.parametrize(
    ("plays", "days"), [(0, 30), (4, 30), (5, 14), (19, 14), (20, 7), (500, 7)]
)
def test_validity_days_at_4_5_19_and_20_plays(plays, days):
    assert service._validity_days(plays) == days


def test_validity_days_follow_the_settings(monkeypatch):
    monkeypatch.setattr(settings, "recommendations_tier_mid_plays", 2)
    monkeypatch.setattr(settings, "recommendations_tier_high_plays", 3)
    monkeypatch.setattr(settings, "recommendations_tier_high_days", 1)
    monkeypatch.setattr(settings, "recommendations_tier_mid_days", 2)
    monkeypatch.setattr(settings, "recommendations_tier_low_days", 3)

    assert [service._validity_days(n) for n in (1, 2, 3)] == [3, 2, 1]


def test_seeds_interleave_favorites_and_listened_without_repeats():
    seeds = service._artist_seed_ids(
        ["UCf1", "UCf2", "UCf3"], ["UCl1", "UCf2", "UCl2", "UCl3"]
    )

    assert seeds == ["UCf1", "UCl1", "UCf2", "UCf3", "UCl2"]


def test_seeds_continue_with_the_longer_list_when_one_runs_out():
    assert service._artist_seed_ids(["UCf1"], ["UCl1", "UCl2", "UCl3"]) == [
        "UCf1",
        "UCl1",
        "UCl2",
        "UCl3",
    ]


def test_seed_ids_outside_the_route_patterns_are_skipped():
    seeds = service._artist_seed_ids(
        ["not-an-artist", "MPLAUCf2", "UCf3"], ["MPREb_album", "UCl1"]
    )

    assert seeds == ["MPLAUCf2", "UCl1", "UCf3"]


def _items(prefix, count):
    return [SimpleNamespace(id=f"{prefix}{i}") for i in range(1, count + 1)]


def test_artist_rounds_take_the_fourth_similar_of_each_seed_before_a_fifth():
    sources = [_items("a", 10), _items("b", 10), _items("c", 10)]

    picked = service._rounds(sources, 12, set())

    assert [(i, item.id) for i, item in picked] == [
        (0, "a1"),
        (1, "b1"),
        (2, "c1"),
        (0, "a2"),
        (1, "b2"),
        (2, "c2"),
        (0, "a3"),
        (1, "b3"),
        (2, "c3"),
        (0, "a4"),
        (1, "b4"),
        (2, "c4"),
    ]


def test_artist_rounds_never_fill_from_a_single_seed_while_others_have_candidates():
    sources = [_items("a", 10), _items("b", 3), _items("c", 3)]

    picked = service._rounds(sources, 9, set())

    assert [index for index, _ in picked].count(0) == 3
    assert [index for index, _ in picked].count(1) == 3
    assert [index for index, _ in picked].count(2) == 3


def test_a_single_seed_fills_with_rounds_of_the_same_seed():
    picked = service._rounds([_items("a", 20)], 15, set())

    assert [item.id for _, item in picked] == [f"a{i}" for i in range(1, 16)]


def test_rounds_skip_taken_ids_and_record_what_they_pick():
    taken = {"a1", "b1"}
    sources = [_items("a", 3), _items("b", 3)]

    picked = service._rounds(sources, 4, taken)

    assert [item.id for _, item in picked] == ["a2", "b2", "a3", "b3"]
    assert taken == {"a1", "a2", "a3", "b1", "b2", "b3"}


def test_rounds_stop_when_the_sources_run_out():
    picked = service._rounds([_items("a", 2), _items("b", 1)], 15, set())

    assert [item.id for _, item in picked] == ["a1", "b1", "a2"]


def test_rounds_can_be_limited_to_a_number_of_rounds():
    picked = service._rounds([_items("a", 3), _items("b", 3)], 15, set(), max_rounds=1)

    assert [item.id for _, item in picked] == ["a1", "b1"]


def test_parallel_returns_the_results_in_the_input_order():
    def slow_first(n):
        time.sleep(0.05 if n == 1 else 0)
        return n * 10

    assert service._parallel(slow_first, [1, 2, 3, 4]) == [10, 20, 30, 40]


def test_parallel_with_no_items_does_not_start_anything():
    assert service._parallel(lambda n: n, []) == []


def test_parallel_runs_at_most_the_configured_workers(monkeypatch):
    monkeypatch.setattr(settings, "recommendations_parallel_calls", 3)
    lock = threading.Lock()
    state = {"running": 0, "peak": 0}

    def work(n):
        with lock:
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
        time.sleep(0.02)
        with lock:
            state["running"] -= 1
        return n

    results = service._parallel(work, list(range(12)))

    assert results == list(range(12))
    assert state["peak"] <= 3
    assert state["peak"] > 1


def test_parallel_cancels_pending_calls_after_a_failure(monkeypatch):
    monkeypatch.setattr(settings, "recommendations_parallel_calls", 1)
    started = []

    def work(n):
        started.append(n)
        if n == 0:
            raise RuntimeError("boom")
        time.sleep(0.1)
        return n

    with pytest.raises(RuntimeError):
        service._parallel(work, list(range(20)))

    assert started[0] == 0
    # The failure ends the call before the other tasks start; at most the
    # one the single worker had already taken runs.
    assert len(started) <= 2


def test_a_worker_runs_with_the_image_color_scope_detached():
    import contextvars

    def request():
        scope = open_image_color_scope()
        service._parallel(
            lambda n: observe_image(f"k{n}", f"https://host/k{n}=w192-h192-p"), [1, 2]
        )
        observe_image("mine", "https://host/mine=w192-h192-p")
        return scope

    scope = contextvars.copy_context().run(request)

    assert list(scope.seen) == ["mine"]


def _album_refs(prefix, count):
    return [AlbumRef(id=f"{prefix}{i}", title=f"T {prefix}{i}") for i in range(count)]


def _seed(seed_id):
    return RecommendationSeed(type="artist", id=seed_id, name=seed_id)


def test_ranked_albums_reserve_never_exceeds_the_count(monkeypatch):
    monkeypatch.setattr(settings, "recommendations_history_albums", 15)

    picked = service._ranked_albums(
        [],
        [_album_refs("h", 30)],
        [],
        [_seed("album-seed")],
        10,
        set(),
    )

    assert len(picked) == 10


def _page(page_id, albums=(), related=()):
    return Artist(
        id=page_id,
        name=page_id,
        albums=list(albums),
        related=[ArtistRef(id=r, name=r) for r in related],
    )


def _rows_for(seed_pages, similar_pages, chosen):
    return service._build_rows(seed_pages, similar_pages, chosen, [], [])


def test_build_rows_gives_an_album_to_the_first_list_that_asks_for_it():
    shared = AlbumRef(id="shared", title="Shared")
    seed = _page("UCseed")
    similar = _page("UCsim", albums=[shared, *_album_refs("x", 12)])
    chosen = [(0, ArtistRef(id="UCsim", name="UCsim"))]

    rows = _rows_for([seed], {"UCsim": similar}, chosen)

    placed = {
        r["item_id"]: r["payload"]["section"] for r in rows if r["item_type"] == "album"
    }
    # The ranking asks first, so the section never gets it.
    assert placed["shared"] == "albums_for_you"
    sections = [r["payload"]["section"] for r in rows if r["item_type"] == "album"]
    assert sections.count("albums_for_you") == 10
    assert sections.count("seed_sections") == 3
    assert sections.count("recommended_albums") == 0


def test_build_rows_never_repeats_an_artist_or_album_id():
    seeds = [_page(f"UCs{i}") for i in range(5)]
    shared = AlbumRef(id="shared", title="Shared")
    similars = {}
    chosen = []
    for i in range(5):
        for j in range(4):
            sid = f"UCr{i}{j}"
            similars[sid] = _page(sid, albums=[shared, *_album_refs(f"{sid}-", 4)])
            chosen.append((i, ArtistRef(id=sid, name=sid)))

    rows = _rows_for(seeds, similars, chosen)

    artist_ids = [r["item_id"] for r in rows if r["item_type"] == "artist"]
    album_ids = [r["item_id"] for r in rows if r["item_type"] == "album"]
    assert len(artist_ids) == len(set(artist_ids)) == 19
    assert len(album_ids) == len(set(album_ids))
    artist_ranks = [r["rank"] for r in rows if r["item_type"] == "artist"]
    assert artist_ranks == list(range(1, len(artist_ranks) + 1))
    sections = [r["payload"]["section"] for r in rows if r["item_type"] == "artist"]
    assert sections[:9] == ["seed_sections"] * 9
    assert set(sections[9:]) == {"recommended_artists"}
    indexes = [
        r["payload"]["section_index"]
        for r in rows
        if r["payload"]["section"] == "seed_sections"
    ]
    assert set(indexes) == {0, 1, 2}


def test_fit_lists_drops_the_artists_that_fit_no_list():
    # Seed 1 has no candidates: the rounds gave its cupos to the others.
    chosen = [(0, ArtistRef(id=f"UCa{n}", name="a")) for n in range(5)] + [
        (2, ArtistRef(id=f"UCc{n}", name="c")) for n in range(15)
    ]

    fitted = service._fit_lists(chosen, 3)

    # 3 + 3 in the sections, 10 recommended, in the order they were chosen.
    assert len(fitted) == 16
    assert fitted == chosen[:5] + chosen[5:16]


def test_stored_set_with_a_seed_row_without_index_is_an_upstream_error(monkeypatch):
    row = {
        "item_type": "artist",
        "item_id": "UCx",
        "rank": 1,
        "seed_type": "artist",
        "seed_id": "UCs",
        "seed_name": "s",
        "payload": {"name": "x", "section": "seed_sections", "section_index": 0},
        "generated_at": "2026-10-30T00:00:00+00:00",
        "valid_until": "2026-11-30T00:00:00+00:00",
    }
    db = SimpleNamespace()
    query = SimpleNamespace(data=[row])
    chain = SimpleNamespace()
    for name in ("select", "eq", "order", "limit"):
        setattr(chain, name, lambda *a, **k: chain)
    chain.execute = lambda: query
    db.table = lambda name: chain
    # The validator makes this impossible from real data; an unvalidated
    # placement stands for the anomaly.
    monkeypatch.setattr(
        service.StoredPlacement,
        "model_validate",
        lambda payload: service.StoredPlacement.model_construct(
            section="seed_sections", section_index=None
        ),
    )

    with pytest.raises(service.UpstreamError):
        service._stored_set(db, "user")


@pytest.mark.parametrize(
    ("title", "base"),
    [
        ("Whatever's Clever! (Expanded)", "whatever's clever!"),
        ("Whatever's Clever!", "whatever's clever!"),
        ("A Wonderful Life (Epilogue)", "a wonderful life"),
        ("A Wonderful Life", "a wonderful life"),
        ("Album (10th Anniversary Edition)", "album"),
        ("Album (Deluxe) [Remastered]", "album"),
        ("  ALBUM   (Deluxe) ", "album"),
        ("(What's the Story) Morning Glory?", "(what's the story) morning glory?"),
        ("(Untitled)", "(untitled)"),
    ],
)
def test_base_title_strips_every_trailing_bracketed_suffix(title, base):
    assert service._base_title(title) == base


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("BEEF Season 2 (Original Soundtrack)", True),
        ("A Very Jonas Christmas Movie (Original Soundtrack)", True),
        ("Stranger Things OST", True),
        ("[ost]", True),
        ("original soundtrack", True),
        ("Ghost", False),
        ("Ghosts", False),
        ("Hostage", False),
        ("Lost", False),
        ("Boston", False),
        ("Postcards", False),
        ("Soundtracks", False),
        ("Soundtracking", False),
    ],
)
def test_soundtrack_titles_match_whole_words_in_any_case(title, expected):
    assert service._is_soundtrack(title) is expected


def test_rounds_skip_an_item_when_any_of_its_keys_is_taken():
    sources = [_items("a", 3), _items("b", 3)]
    shared = {"a1": ("ed", "x"), "b1": ("ed", "x")}

    def keys(item):
        return (item.id, *([shared[item.id]] if item.id in shared else []))

    taken: set = set()
    picked = service._rounds(sources, 3, taken, keys=keys)

    assert [item.id for _, item in picked] == ["a1", "b2", "a2"]
    assert taken == {"a1", "a2", "b2", ("ed", "x")}


def _album_page(page_id, title, artists=(), related=()):
    return Album(
        id=page_id,
        title=title,
        artists=[AlbumArtistRef(id=a, name=a) for a in artists],
        duration_seconds=0,
        tracks=[],
        related_recommendations=list(related),
    )


def _ref(album_id, title, artists=()):
    return AlbumRef(
        id=album_id,
        title=title,
        artists=[AlbumArtistRef(id=a, name=a) for a in artists],
    )


def _build(similars, album_pages=()):
    # One artist seed; every similar is chosen for it.
    seed = _page("UCseed")
    pages = {s.id: s for s in similars}
    chosen = [(0, ArtistRef(id=s.id, name=s.name)) for s in similars]
    seed_ids = [p.id for p in album_pages]
    return service._build_rows([seed], pages, chosen, list(album_pages), seed_ids)


def _album_ids(rows):
    return [r["item_id"] for r in rows if r["item_type"] == "album"]


def _count(rows, section):
    return [r["payload"]["section"] for r in rows if r["item_type"] == "album"].count(
        section
    )


def test_build_rows_keeps_one_edition_of_an_album_and_fills_its_slot():
    similar = _page(
        "UCsim",
        albums=[
            _ref("exp", "Whatever's Clever! (Expanded)"),
            _ref("plain", "Whatever's Clever!"),
            *_album_refs("x", 12),
        ],
    )

    rows = _build([similar])

    ids = _album_ids(rows)
    assert "exp" in ids
    assert "plain" not in ids
    assert _count(rows, "albums_for_you") == 10
    assert _count(rows, "seed_sections") == 3


def _section_of(rows, album_id):
    return next(
        r["payload"]["section"]
        for r in rows
        if r["item_type"] == "album" and r["item_id"] == album_id
    )


@pytest.mark.parametrize(
    ("life_index", "life_section"),
    [(0, "albums_for_you"), (5, "seed_sections")],
)
def test_build_rows_drops_an_edition_that_a_later_list_asks_for(
    life_index, life_section
):
    # The other edition sits deep in the second page, so only a later list
    # (recommended_albums) reaches it, after another list took the first one.
    first_albums = _album_refs("a", 20)
    first_albums.insert(life_index, _ref("life", "A Wonderful Life", ["UCx"]))
    second_albums = _album_refs("b", 20)
    second_albums.insert(8, _ref("epilogue", "A Wonderful Life (Epilogue)", ["UCx"]))

    rows = _build(
        [_page("UCfirst", albums=first_albums), _page("UCsecond", albums=second_albums)]
    )

    ids = _album_ids(rows)
    assert _section_of(rows, "life") == life_section
    assert "epilogue" not in ids
    assert _count(rows, "albums_for_you") == 10
    assert _count(rows, "seed_sections") == 3
    assert _count(rows, "recommended_albums") == 10


def test_build_rows_edition_reaches_recommended_albums_only_if_none_took_it():
    # Control: with no other edition taken, the same deep album is placed.
    second_albums = _album_refs("b", 20)
    second_albums.insert(8, _ref("epilogue", "A Wonderful Life (Epilogue)", ["UCx"]))

    rows = _build(
        [
            _page("UCfirst", albums=_album_refs("a", 20)),
            _page("UCsecond", albums=second_albums),
        ]
    )

    assert _section_of(rows, "epilogue") == "recommended_albums"


@pytest.mark.parametrize("linked", ["MPLAUCa", "UCa"])
def test_build_rows_matches_editions_across_sources_by_the_artist(linked):
    similar = _page("UCa", albums=[_ref("x", "X"), *_album_refs("f", 12)])
    seed_album = _album_page(
        "MPREb_seed",
        "Seed Album",
        related=[_ref("xd", "X (Deluxe)", [linked]), *_album_refs("r", 12)],
    )

    rows = _build([similar], [seed_album])

    ids = _album_ids(rows)
    assert ("x" in ids) != ("xd" in ids)


def test_build_rows_keeps_same_titles_of_different_artists():
    sims = [
        _page("UCa", albums=[_ref("gha", "Greatest Hits"), *_album_refs("a", 3)]),
        _page("UCb", albums=[_ref("ghb", "Greatest Hits"), *_album_refs("b", 3)]),
        _page(
            "UCc", albums=[_ref("ghc", "Greatest Hits (Deluxe)"), *_album_refs("c", 3)]
        ),
    ]

    ids = _album_ids(_build(sims))

    assert {"gha", "ghb", "ghc"} <= set(ids)


def test_build_rows_leaves_soundtracks_out_and_fills_their_slots():
    similar = _page(
        "UCsim",
        albums=[
            _ref("beef", "BEEF Season 2 (Original Soundtrack)"),
            _ref("jonas", "A Very Jonas Christmas Movie (Original Soundtrack)"),
            _ref("ghost", "Ghost"),
            *_album_refs("x", 12),
        ],
    )
    seed_album = _album_page(
        "MPREb_seed",
        "Seed Album",
        related=[_ref("st", "Stranger Things OST"), *_album_refs("r", 3)],
    )

    rows = _build([similar], [seed_album])

    ids = _album_ids(rows)
    assert not {"beef", "jonas", "st"} & set(ids)
    assert "ghost" in ids
    assert _count(rows, "albums_for_you") == 10


def test_build_rows_never_recommends_another_edition_of_an_album_seed():
    seed_album = _album_page("MPREb_y", "Y", artists=["UCb"])
    similar = _page("UCb", albums=[_ref("yd", "Y (Deluxe)"), *_album_refs("x", 12)])

    ids = _album_ids(_build([similar], [seed_album]))

    assert "yd" not in ids
    assert "MPREb_y" not in ids
