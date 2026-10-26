# test/core/test_image_color_scope.py
#
# Tests for the per-request image color scope.
#
# Tested:
# - Without a scope observe_image returns None and records nothing
# - With a scope it records the first URL per key and returns the loaded
#   color
# - A scope opened in one context is not seen in another
#
# What is covered:
# - core/image_color_scope.py
#
# Run with: pytest test/core/test_image_color_scope.py -v
#
# SEE: core/image_color_scope.py

import contextvars

from core.image_color_scope import observe_image, open_image_color_scope


def test_without_a_scope_observe_returns_none_and_records_nothing():
    assert observe_image("k", "https://host/k=w192-h192-p") is None


def test_with_a_scope_the_first_url_per_key_wins_and_the_color_is_returned():
    def inside():
        scope = open_image_color_scope()
        scope.colors["k"] = "#ee0000"
        first = observe_image("k", "https://host/k=w192-h192-p")
        second = observe_image("k", "https://other/k=w192-h192-p")
        unknown = observe_image("z", "https://host/z=w192-h192-p")
        return scope, first, second, unknown

    scope, first, second, unknown = contextvars.copy_context().run(inside)

    assert first == second == "#ee0000"
    assert unknown is None
    assert scope.seen == {
        "k": "https://host/k=w192-h192-p",
        "z": "https://host/z=w192-h192-p",
    }


def test_a_scope_opened_in_one_context_is_not_seen_in_another():
    contextvars.copy_context().run(open_image_color_scope)

    assert observe_image("k", "https://host/k=w192-h192-p") is None
