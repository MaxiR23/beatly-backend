# test/services/test_onboarding_service.py
#
# Tests for the helpers of the onboarding service that need no database or
# provider: the copies of the parallel helpers of the recommendations service.
#
# Tested:
# - _parallel returns the results in the input order, runs at most four
#   workers at once, does not start anything without items and does not
#   start the pending calls after a failure
# - A worker runs with the image color scope detached
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

import pytest

from core.image_color_scope import observe_image, open_image_color_scope
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
