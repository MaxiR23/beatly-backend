# test/services/test_onboarding_service.py
#
# Tests for the helpers of the onboarding service that need no database and
# the selection of similar artists, with a fake provider.
#
# Tested:
# - _parallel returns the results in the input order, runs at most four
#   workers at once, does not start anything without items and does not
#   start the pending calls after a failure
# - A worker runs with the image color scope detached
# - similar_artists keeps the provider order, skips the excluded ids, the
#   artist itself and the ids outside the artist pattern, fills up to three,
#   treats MPLA and plain ids as the same artist and returns a repeated
#   related id once
#
# What is covered:
# - services/onboarding_service.py helpers; the endpoints are in
#   test/routes/test_onboarding.py
#
# Run with: pytest test/services/test_onboarding_service.py -v
#
# SEE: services/onboarding_service.py

import contextvars
import threading
import time
from unittest.mock import MagicMock

import pytest

from core.image_color_scope import observe_image, open_image_color_scope
from models.artist import Artist, ArtistRef
from services import onboarding_service as service


def test_parallel_returns_the_results_in_the_input_order():
    def slow_first(n):
        time.sleep(0.05 if n == 1 else 0)
        return n * 10

    assert service._parallel(slow_first, [1, 2, 3, 4]) == [10, 20, 30, 40]


def test_parallel_with_no_items_does_not_start_anything():
    assert service._parallel(lambda n: n, []) == []


def test_parallel_runs_at_most_four_workers():
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

    results = service._parallel(work, list(range(20)))

    assert results == list(range(20))
    assert state["peak"] <= 4
    assert state["peak"] > 1


def test_parallel_cancels_pending_calls_after_a_failure(monkeypatch):
    monkeypatch.setattr(service, "_PARALLEL_CALLS", 1)
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
    def request():
        scope = open_image_color_scope()
        service._parallel(
            lambda n: observe_image(f"k{n}", f"https://host/k{n}=w192-h192-p"), [1, 2]
        )
        observe_image("mine", "https://host/mine=w192-h192-p")
        return scope

    scope = contextvars.copy_context().run(request)

    assert list(scope.seen) == ["mine"]


def _similar(related_ids, artist_id="UCseed", exclude_ids=()):
    page = Artist(
        id=artist_id,
        name="Seed",
        related=[ArtistRef(id=i, name=f"Name {i}") for i in related_ids],
    )
    cache = MagicMock()
    cache.get.return_value = page.model_dump_json()
    cache.ttl.return_value = 1234
    result = service.similar_artists(MagicMock(), cache, artist_id, list(exclude_ids))
    return [a.id for a in result.artists]


def test_similar_artists_keeps_the_provider_order_and_stops_at_three():
    assert _similar(["UC1", "UC2", "UC3", "UC4"]) == ["UC1", "UC2", "UC3"]


def test_similar_artists_skips_excluded_and_fills_with_the_next():
    assert _similar(["UC1", "UC2", "UC3", "UC4", "UC5"], exclude_ids=["UC2"]) == [
        "UC1",
        "UC3",
        "UC4",
    ]


@pytest.mark.parametrize("excluded", [[], ["UCseed"]])
def test_similar_artists_never_returns_the_artist_itself(excluded):
    assert _similar(["UC1", "UCseed", "UC2", "UC3"], exclude_ids=excluded) == [
        "UC1",
        "UC2",
        "UC3",
    ]


def test_similar_artists_skips_a_related_id_outside_the_artist_pattern():
    assert _similar(["UC1", "OLAK5uy_x", "UC2", "UC3"]) == ["UC1", "UC2", "UC3"]


def test_similar_artists_treats_mpla_and_plain_ids_as_the_same_artist():
    assert _similar(["MPLAUC1", "UC2", "UC3", "UC4"], exclude_ids=["UC1"]) == [
        "UC2",
        "UC3",
        "UC4",
    ]
    assert _similar(["UC1", "UC2"], artist_id="UCseed", exclude_ids=["MPLAUC1"]) == [
        "UC2"
    ]


def test_similar_artists_returns_a_repeated_related_id_once():
    assert _similar(["UC1", "UC1", "MPLAUC1", "UC2"]) == ["UC1", "UC2"]
