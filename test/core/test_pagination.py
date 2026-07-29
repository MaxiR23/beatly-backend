# test/core/test_pagination.py
#
# Tests for the shared cursor pagination helper.
#
# Tested:
# - A cursor round-trips its sort value and its id tiebreaker, for a
#   timestamp and a numeric sort key
# - The cursor is opaque: the raw id is not readable in it, and a payload
#   without the id tiebreaker is rejected
# - A malformed, truncated or wrong-shape cursor raises invalid_cursor
#   (422), and never leaks why
# - A value the declared ValueType could not hold is invalid_cursor too:
#   wrong Python type, a timestamp that does not parse, an id that is not
#   a uuid, an int past bigint, a non-finite or oversized float
#   - all raised before the query is built, so none can surface as
#     upstream_error
# - A TEXT value that cannot make the trip back — a lone surrogate, or a
#   NUL Postgres text cannot hold — is invalid_cursor
# - A TEXT id tiebreaker accepts a provider id, the likes case
# - A float sort key accepts the int JSON gives for a whole number
# - limit defaults to 50 and accepts 1..100
# - limit 0, -5, 101 or non-numeric returns 422 invalid_request
# - A garbage or mistyped cursor query param returns 422 invalid_cursor
# - The keyset filter is "key < X OR (key = X AND id < Y)" descending and
#   the > form ascending, with the id tiebreaker in both
# - A sort value containing a comma is quoted, so the filter cannot split
# - apply_page orders by sort key then id, fetches limit+1, and only
#   filters when a cursor is present
# - The first page carries the exact total; a cursored page carries none,
#   and each of the two misuses raises instead of building a page
# - has_more comes from the limit+1 row, which is trimmed off the page
# - End of the collection is has_more false with next_cursor null
# - next_cursor points at the last kept row, not the trimmed probe row
# - next_cursor is emitted through the same normalizer decode reads it
#   with, so it round-trips; a row value that would not survive that
#   round trip raises instead of becoming a poison cursor
#
# What is covered:
# - Cursor round-trip, rejection and per-type value validation, limit
#   validation, keyset filter shape in both directions, page block
#   derivation and its misuse, end of collection
#
# Run with: pytest test/core/test_pagination.py -v
#
# SEE: core/pagination.py, models/responses.py, docs/api/conventions.md

import base64
import json
from typing import Annotated
from unittest.mock import MagicMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from app import app_error_handler, validation_error_handler
from core.exceptions import AppError, InvalidRequest
from core.pagination import (
    DEFAULT_LIMIT,
    Cursor,
    PageRequest,
    SortKey,
    ValueType,
    apply_page,
    build_page,
    decode_cursor,
    encode_cursor,
    keyset_filter,
    page_params,
)

_TIMESTAMP = "2026-01-01T00:00:00+00:00"
_ROW_ID = "11111111-1111-1111-1111-111111111111"

_CREATED_DESC = SortKey("created_at", ValueType.TIMESTAMP)
_POSITION_ASC = SortKey("position", ValueType.INT, descending=False)
_SCORE_DESC = SortKey("score", ValueType.FLOAT)
# The likes shape: a provider id, not a catalog uuid, as the tiebreaker.
_TEXT_ID = SortKey(
    "created_at",
    ValueType.TIMESTAMP,
    id_column="track_id",
    id_type=ValueType.TEXT,
)


def _uuid(index: int) -> str:
    return f"00000000-0000-0000-0000-{index:012d}"


def _encode_payload(payload) -> str:
    raw = json.dumps(payload).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _row(created_at: str, row_id: str) -> dict:
    return {"created_at": created_at, "id": row_id, "title": "Track"}


def _first_page(limit: int = 10) -> PageRequest:
    return PageRequest(limit=limit)


def _cursored_page(limit: int = 10) -> PageRequest:
    return PageRequest(limit=limit, cursor=encode_cursor(_TIMESTAMP, _ROW_ID))


# A throwaway app, so limit and cursor are exercised through the real
# request path and the real handlers without shipping a route.
def _build_app() -> FastAPI:
    test_app = FastAPI()
    test_app.add_exception_handler(AppError, app_error_handler)
    test_app.add_exception_handler(RequestValidationError, validation_error_handler)

    @test_app.get("/paged")
    def paged(page: Annotated[PageRequest, Depends(page_params)]) -> dict:
        cursor = page.decode(_CREATED_DESC)
        return {"limit": page.limit, "cursor_id": cursor.id if cursor else None}

    @test_app.get("/paged-by-position")
    def paged_by_position(page: Annotated[PageRequest, Depends(page_params)]) -> dict:
        cursor = page.decode(_POSITION_ASC)
        return {"cursor_value": cursor.value if cursor else None}

    return test_app


client = TestClient(_build_app(), raise_server_exceptions=False)


# --- cursor round-trip ---------------------------------------------------


def test_cursor_round_trips_timestamp_sort_value_and_id():
    cursor = decode_cursor(encode_cursor(_TIMESTAMP, _ROW_ID), _CREATED_DESC)

    assert cursor == Cursor(value=_TIMESTAMP, id=_ROW_ID)


def test_cursor_round_trips_numeric_sort_value():
    cursor = decode_cursor(encode_cursor(42, _ROW_ID), _POSITION_ASC)

    assert cursor.value == 42
    assert cursor.id == _ROW_ID


def test_cursor_is_opaque_and_carries_the_id_tiebreaker():
    encoded = encode_cursor(_TIMESTAMP, _ROW_ID)

    assert _ROW_ID not in encoded
    assert _TIMESTAMP not in encoded
    assert decode_cursor(encoded, _CREATED_DESC).id == _ROW_ID


def test_cursor_without_id_tiebreaker_is_rejected():
    with pytest.raises(InvalidRequest):
        decode_cursor(_encode_payload({"k": _TIMESTAMP}), _CREATED_DESC)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not a cursor",
        "!!!!",
        encode_cursor(_TIMESTAMP, _ROW_ID)[:6],
        base64.urlsafe_b64encode(b"not json").decode().rstrip("="),
        _encode_payload([_TIMESTAMP, _ROW_ID]),
        _encode_payload("just a string"),
        _encode_payload({"i": _ROW_ID}),
        _encode_payload({"k": _TIMESTAMP, "i": _ROW_ID, "extra": 1}),
        _encode_payload({"k": _TIMESTAMP, "i": 7}),
        _encode_payload({"k": None, "i": _ROW_ID}),
        _encode_payload({"k": True, "i": _ROW_ID}),
        _encode_payload({"k": {"nested": 1}, "i": _ROW_ID}),
    ],
)
def test_malformed_cursor_raises_invalid_cursor(raw):
    with pytest.raises(InvalidRequest) as exc_info:
        decode_cursor(raw, _CREATED_DESC)

    assert exc_info.value.status_code == 422
    assert exc_info.value.reason == "invalid_cursor"


# --- cursor value types --------------------------------------------------


@pytest.mark.parametrize(
    ("value", "value_type"),
    [
        # Wrong Python type for the declared column.
        (_TIMESTAMP, ValueType.INT),
        ("12", ValueType.INT),
        (42, ValueType.TEXT),
        (42, ValueType.TIMESTAMP),
        (1.5, ValueType.INT),
        (_TIMESTAMP, ValueType.FLOAT),
        # Right type, value the column could not hold.
        ("not-a-timestamp", ValueType.TIMESTAMP),
        ("2026-13-45T00:00:00Z", ValueType.TIMESTAMP),
        ("", ValueType.TIMESTAMP),
        ("not-a-uuid", ValueType.UUID),
        ("11111111-1111-1111-1111", ValueType.UUID),
        (2**31, ValueType.INT),
        (-(2**31) - 1, ValueType.INT),
    ],
)
def test_value_the_declared_type_cannot_hold_is_invalid_cursor(value, value_type):
    sort = SortKey("sort_key", value_type, id_type=ValueType.TEXT)

    with pytest.raises(InvalidRequest) as exc_info:
        decode_cursor(encode_cursor(value, "any-id"), sort)

    assert exc_info.value.status_code == 422
    assert exc_info.value.reason == "invalid_cursor"


@pytest.mark.parametrize("value_type", [ValueType.FLOAT, ValueType.INT])
def test_integer_too_large_for_the_column_is_invalid_cursor(value_type):
    # math.isfinite raises OverflowError on an int this size, so the range
    # check has to come first: the answer is a bad cursor, not a 500.
    raw = _encode_payload({"k": 10**1000, "i": _ROW_ID})
    sort = SortKey("sort_key", value_type)

    with pytest.raises(InvalidRequest) as exc_info:
        decode_cursor(raw, sort)

    assert exc_info.value.reason == "invalid_cursor"


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"]
)
def test_non_finite_float_cursor_value_is_invalid_cursor(value):
    raw = _encode_payload({"k": value, "i": _ROW_ID})

    with pytest.raises(InvalidRequest) as exc_info:
        decode_cursor(raw, _SCORE_DESC)

    assert exc_info.value.status_code == 422
    assert exc_info.value.reason == "invalid_cursor"


def test_float_sort_key_accepts_the_int_json_gives_a_whole_number():
    cursor = decode_cursor(encode_cursor(3, _ROW_ID), _SCORE_DESC)

    assert cursor.value == 3


@pytest.mark.parametrize(
    "value", ["\ud800", "with\x00nul"], ids=["lone-surrogate", "nul"]
)
def test_text_value_that_cannot_be_transported_is_invalid_cursor(value):
    # json.loads reads both, but neither survives the trip back out: a lone
    # surrogate cannot be encoded, and Postgres text cannot hold a NUL.
    sort = SortKey("title", ValueType.TEXT, id_type=ValueType.TEXT)
    raw = _encode_payload({"k": value, "i": "track-abc"})

    with pytest.raises(InvalidRequest) as exc_info:
        decode_cursor(raw, sort)

    assert exc_info.value.status_code == 422
    assert exc_info.value.reason == "invalid_cursor"


def test_iso_week_date_value_is_normalized_into_the_filter():
    # Python reads an ISO-8601 week date, Postgres does not, and the cursor's
    # string is what the filter carries.
    cursor = decode_cursor(encode_cursor("2026-W01-1", _ROW_ID), _CREATED_DESC)

    assert cursor.value == "2025-12-29T00:00:00"
    assert '"2025-12-29T00:00:00"' in keyset_filter(_CREATED_DESC, cursor)
    assert "2026-W01-1" not in keyset_filter(_CREATED_DESC, cursor)


def test_urn_uuid_id_tiebreaker_is_normalized_into_the_filter():
    encoded = encode_cursor(_TIMESTAMP, f"urn:uuid:{_ROW_ID.upper()}")

    cursor = decode_cursor(encoded, _CREATED_DESC)

    assert cursor.id == _ROW_ID
    assert f'id.lt."{_ROW_ID}"' in keyset_filter(_CREATED_DESC, cursor)
    assert "urn:uuid:" not in keyset_filter(_CREATED_DESC, cursor)


def test_id_tiebreaker_that_is_not_a_uuid_is_invalid_cursor():
    with pytest.raises(InvalidRequest) as exc_info:
        decode_cursor(encode_cursor(_TIMESTAMP, "not-a-uuid"), _CREATED_DESC)

    assert exc_info.value.reason == "invalid_cursor"


def test_text_id_tiebreaker_accepts_a_provider_id():
    cursor = decode_cursor(encode_cursor(_TIMESTAMP, "track-abc"), _TEXT_ID)

    assert cursor.id == "track-abc"


def test_mistyped_cursor_is_rejected_before_the_query_is_built():
    query = MagicMock()
    request = PageRequest(limit=10, cursor=encode_cursor("not-a-position", _ROW_ID))

    with pytest.raises(InvalidRequest) as exc_info:
        apply_page(query, _POSITION_ASC, request)

    # Never an upstream_error: nothing was sent, so nothing failed upstream.
    assert exc_info.value.reason == "invalid_cursor"
    query.or_.assert_not_called()
    query.order.assert_not_called()
    query.execute.assert_not_called()


def test_a_cursor_valid_for_one_sort_key_can_be_invalid_for_another():
    request = PageRequest(limit=10, cursor=encode_cursor(_TIMESTAMP, _ROW_ID))

    assert request.decode(_CREATED_DESC) == Cursor(value=_TIMESTAMP, id=_ROW_ID)
    with pytest.raises(InvalidRequest):
        request.decode(_POSITION_ASC)


# --- limit and cursor as query params ------------------------------------


def test_absent_limit_defaults_to_fifty():
    response = client.get("/paged")

    assert response.status_code == 200
    assert response.json()["limit"] == DEFAULT_LIMIT == 50


@pytest.mark.parametrize("limit", [1, 50, 100])
def test_limit_within_range_is_accepted(limit):
    response = client.get("/paged", params={"limit": limit})

    assert response.status_code == 200
    assert response.json()["limit"] == limit


@pytest.mark.parametrize("limit", [0, -5, 101, 1000, "abc", 1.5])
def test_limit_out_of_range_or_non_numeric_returns_invalid_request(limit):
    response = client.get("/paged", params={"limit": limit})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}


def test_valid_cursor_param_is_decoded():
    encoded = encode_cursor(_TIMESTAMP, _ROW_ID)

    response = client.get("/paged", params={"cursor": encoded})

    assert response.status_code == 200
    assert response.json()["cursor_id"] == _ROW_ID


def test_garbage_cursor_param_returns_invalid_cursor():
    response = client.get("/paged", params={"cursor": "not-a-real-cursor"})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_cursor"}


def test_cursor_param_of_the_wrong_type_returns_invalid_cursor():
    encoded = encode_cursor(_TIMESTAMP, _ROW_ID)

    response = client.get("/paged-by-position", params={"cursor": encoded})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_cursor"}


def test_empty_cursor_param_is_treated_as_the_first_page():
    response = client.get("/paged", params={"cursor": ""})

    assert response.status_code == 200
    assert response.json()["cursor_id"] is None


# --- keyset filter -------------------------------------------------------


def test_descending_filter_is_key_lower_or_key_equal_and_id_lower():
    condition = keyset_filter(_CREATED_DESC, Cursor(value=_TIMESTAMP, id=_ROW_ID))

    assert condition == (
        f'created_at.lt."{_TIMESTAMP}",'
        f'and(created_at.eq."{_TIMESTAMP}",id.lt."{_ROW_ID}")'
    )


def test_ascending_filter_is_key_greater_or_key_equal_and_id_greater():
    condition = keyset_filter(_POSITION_ASC, Cursor(value=3, id=_ROW_ID))

    assert condition == f'position.gt.3,and(position.eq.3,id.gt."{_ROW_ID}")'


def test_filter_carries_the_id_tiebreaker_the_cursor_encoded():
    cursor = decode_cursor(encode_cursor(_TIMESTAMP, _ROW_ID), _CREATED_DESC)

    condition = keyset_filter(_CREATED_DESC, cursor)

    assert f'id.lt."{_ROW_ID}"' in condition
    assert cursor.id == _ROW_ID


def test_filter_names_the_declared_id_column():
    condition = keyset_filter(_TEXT_ID, Cursor(value=_TIMESTAMP, id="track-abc"))

    assert 'track_id.lt."track-abc"' in condition


def test_sort_value_containing_a_comma_is_quoted():
    sort = SortKey("title", ValueType.TEXT)

    condition = keyset_filter(sort, Cursor(value="Hits, Vol. 2", id=_ROW_ID))

    assert condition.startswith('title.lt."Hits, Vol. 2",')


# --- apply_page ----------------------------------------------------------


def test_apply_page_without_cursor_orders_by_key_then_id_and_overfetches():
    query = MagicMock()

    result = apply_page(query, _CREATED_DESC, _first_page())

    query.or_.assert_not_called()
    query.order.assert_called_once_with("created_at", desc=True)
    ordered = query.order.return_value
    ordered.order.assert_called_once_with("id", desc=True)
    ordered.order.return_value.limit.assert_called_once_with(11)
    assert result is ordered.order.return_value.limit.return_value


def test_apply_page_with_cursor_applies_the_keyset_filter_once():
    query = MagicMock()

    apply_page(query, _CREATED_DESC, _cursored_page())

    cursor = Cursor(value=_TIMESTAMP, id=_ROW_ID)
    query.or_.assert_called_once_with(keyset_filter(_CREATED_DESC, cursor))
    query.or_.return_value.order.assert_called_once_with("created_at", desc=True)


def test_apply_page_ascending_orders_both_columns_ascending():
    query = MagicMock()

    apply_page(query, _POSITION_ASC, PageRequest(limit=5))

    query.order.assert_called_once_with("position", desc=False)
    query.order.return_value.order.assert_called_once_with("id", desc=False)


# --- page block ----------------------------------------------------------


def test_first_page_reports_the_total():
    rows = [_row(_TIMESTAMP, _uuid(0)), _row(_TIMESTAMP, _uuid(1))]

    _, page = build_page(rows, _first_page(), _CREATED_DESC, 370)

    assert page.total == 370
    assert page.limit == 10


def test_cursored_page_reports_total_null():
    rows = [_row(_TIMESTAMP, _uuid(0))]

    _, page = build_page(rows, _cursored_page(), _CREATED_DESC, None)

    assert page.total is None


def test_first_page_without_a_total_is_refused():
    rows = [_row(_TIMESTAMP, _uuid(0))]

    with pytest.raises(ValueError):
        build_page(rows, _first_page(), _CREATED_DESC, None)


def test_cursored_page_carrying_a_total_is_refused():
    rows = [_row(_TIMESTAMP, _uuid(0))]

    with pytest.raises(ValueError):
        build_page(rows, _cursored_page(), _CREATED_DESC, 370)


def test_count_mode_is_requested_only_on_the_first_page():
    assert _first_page().count_mode == "exact"
    assert _cursored_page().count_mode is None


def test_extra_row_means_has_more_and_is_trimmed_off_the_page():
    rows = [_row(_TIMESTAMP, _uuid(index)) for index in range(3)]

    items, page = build_page(rows, _first_page(limit=2), _CREATED_DESC, 3)

    assert len(items) == 2
    assert [item["id"] for item in items] == [_uuid(0), _uuid(1)]
    assert page.has_more is True
    assert page.next_cursor is not None


def test_full_page_without_extra_row_is_the_end_of_the_collection():
    rows = [_row(_TIMESTAMP, _uuid(index)) for index in range(2)]

    items, page = build_page(rows, _first_page(limit=2), _CREATED_DESC, 2)

    assert len(items) == 2
    assert page.has_more is False
    assert page.next_cursor is None


def test_empty_result_has_no_next_cursor():
    items, page = build_page([], _first_page(), _CREATED_DESC, 0)

    assert items == []
    assert page.has_more is False
    assert page.next_cursor is None
    assert page.total == 0


def test_emitted_cursor_round_trips_through_decode():
    # The row carries the Z form, the cursor the canonical one: what goes out
    # is what decode reads back, so a page can always be continued.
    rows = [_row("2026-01-02T00:00:00Z", _uuid(1)), _row(_TIMESTAMP, _uuid(2))]

    _, page = build_page(rows, _first_page(limit=1), _CREATED_DESC, 2)

    assert decode_cursor(page.next_cursor, _CREATED_DESC) == Cursor(
        value="2026-01-02T00:00:00+00:00", id=_uuid(1)
    )


def test_row_with_a_non_finite_sort_value_is_refused():
    rows = [
        {"score": 1.0, "id": _uuid(1)},
        {"score": float("inf"), "id": _uuid(2)},
        {"score": 0.5, "id": _uuid(3)},
    ]

    with pytest.raises(ValueError) as exc_info:
        build_page(rows, _first_page(limit=2), _SCORE_DESC, 3)

    # Corrupt data on our side, not a bad request: never the 422 path.
    assert not isinstance(exc_info.value, AppError)


def test_row_with_an_infinity_timestamp_is_refused():
    rows = [
        _row(_TIMESTAMP, _uuid(1)),
        _row("infinity", _uuid(2)),
        _row(_TIMESTAMP, _uuid(3)),
    ]

    with pytest.raises(ValueError):
        build_page(rows, _first_page(limit=2), _CREATED_DESC, 3)


def test_row_with_an_id_that_is_not_a_uuid_is_refused():
    rows = [_row(_TIMESTAMP, "not-a-uuid"), _row(_TIMESTAMP, _uuid(2))]

    with pytest.raises(ValueError):
        build_page(rows, _first_page(limit=1), _CREATED_DESC, 2)


def test_next_cursor_points_at_the_last_kept_row_not_the_probe_row():
    rows = [
        _row("2026-01-03T00:00:00+00:00", _uuid(1)),
        _row("2026-01-02T00:00:00+00:00", _uuid(2)),
        _row("2026-01-01T00:00:00+00:00", _uuid(3)),
    ]

    _, page = build_page(rows, _first_page(limit=2), _CREATED_DESC, 3)

    assert decode_cursor(page.next_cursor, _CREATED_DESC) == Cursor(
        value="2026-01-02T00:00:00+00:00", id=_uuid(2)
    )
