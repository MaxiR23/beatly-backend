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
