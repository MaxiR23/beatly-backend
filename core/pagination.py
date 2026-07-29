# INFO: Shared cursor (keyset) pagination helper.

# Every list that can grow paginates through here. A domain declares a
# SortKey and nothing else: the cursor format, the composite filter, the
# limit+1 probe and the page block are built in one place, so two endpoints
# cannot disagree on what has_more or total mean.
#
# Keyset and not offset: an offset makes the database walk and discard every
# skipped row, so page 50 costs more than page 1, and a row inserted between
# two requests shifts the window, duplicating or dropping items. Comparing
# against the last row of the previous page has neither problem.
#
# A cursor is decoded against the SortKey it will be compared with, never on
# its own: only the sort key knows what the value it carries has to be for
# the column it is about to be compared with. So the route dependency keeps
# the cursor raw and apply_page decodes it, which means a Cursor object only
# ever exists once it is valid for the query it is about to filter.
#
# A cursor the database would choke on is a bad cursor, not a provider
# failure: everything a column cannot hold — a timestamp that is not a
# timestamp, an id that is not a uuid, a number out of range — is 422
# invalid_cursor here, before anything is sent, and never a 502 upstream_error
# after PostgREST rejects it.
#
# The sort column must be NOT NULL, and the pair (sort column, id) must be
# selected by the query. A NULL never satisfies < or >, so rows with a null
# sort key would silently vanish after the first page.
#
# ValueType.INT means an int4 column: every integer sort key here is a small
# counter (position caps at 1000). A bigint one would join the enum as INT64
# rather than widen this bound and stop rejecting int4 overflow.
#
# SEE: docs/api/conventions.md (Pagination)

import base64
import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, TypeVar

from fastapi import Query

from core.exceptions import InvalidRequest
from models.responses import PageBlock

DEFAULT_LIMIT = 50
MAX_LIMIT = 100

_INVALID_CURSOR = "invalid_cursor"

CursorValue = str | int | float

# Postgres int4, and the widest double. A cursor past either is one the
# column could never have produced, and sending it would come back as a
# provider error rather than the bad cursor it is.
_INT_MIN, _INT_MAX = -(2**31), 2**31 - 1
_FLOAT_MAX = 1.7976931348623157e308

QueryT = TypeVar("QueryT")


class ValueType(StrEnum):
    """The column types a cursor can carry. Closed on purpose: every sort key
    in this API is one of these, and each one fixes its own validation, so a
    domain declares a meaning rather than supplying a checker."""

    TIMESTAMP = "timestamp"
    INT = "int"
    FLOAT = "float"
    TEXT = "text"
    UUID = "uuid"


@dataclass(frozen=True)
class Cursor:
    value: CursorValue
    id: str


@dataclass(frozen=True)
class SortKey:
    """What a domain declares. Everything else is derived from it."""

    column: str
    # No default: a wrong guess about what the column holds is exactly the
    # mistake that turns a bad cursor into a database error, so every domain
    # states it.
    value_type: ValueType
    descending: bool = True
    id_column: str = "id"
    # Catalog ids are uuids. A domain whose tiebreaker is a provider id
    # (likes, keyed on track_id) declares TEXT instead.
    id_type: ValueType = ValueType.UUID


@dataclass(frozen=True)
class PageRequest:
    limit: int
    # Raw and opaque. It stays a string until a SortKey says what it should
    # contain: see decode().
    cursor: str | None = None

    @property
    def is_first_page(self) -> bool:
        return not self.cursor

    @property
    def fetch_limit(self) -> int:
        # One row past the page. Its presence is has_more, which is why
        # nothing here needs a second query to know there is a next page.
        return self.limit + 1

    @property
    def count_mode(self) -> str | None:
        # total is exact and first-page only, so only that request pays for
        # the count. Services pass this straight to .select(count=...).
        return "exact" if self.is_first_page else None

    def decode(self, sort: SortKey) -> Cursor | None:
        if self.is_first_page:
            return None

        return decode_cursor(self.cursor, sort)


def encode_cursor(value: CursorValue, row_id: str) -> str:
    payload = json.dumps({"k": value, "i": row_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_cursor(raw: str, sort: SortKey) -> Cursor:
    """Every way a cursor can be wrong — unreadable, wrong shape, or a value
    the sort key's column could not hold — is the same 422 invalid_cursor. The
    client cannot build a cursor, so it cannot act on the difference, and the
    original never reaches the response."""
    try:
        padded = raw + "=" * (-len(raw) % 4)
        decoded = base64.b64decode(padded.encode(), altchars=b"-_", validate=True)
        payload = json.loads(decoded)
    # binascii.Error, JSONDecodeError and UnicodeDecodeError are all
    # ValueError.
    except ValueError as exc:
        raise InvalidRequest(_INVALID_CURSOR) from exc

    return _cursor_from_payload(payload, sort)


def _cursor_from_payload(payload: Any, sort: SortKey) -> Cursor:
    if not isinstance(payload, dict) or payload.keys() != {"k", "i"}:
        raise InvalidRequest(_INVALID_CURSOR)

    value, row_id = payload["k"], payload["i"]

    # bool is an int subclass and would compare as 0/1 against a real column.
    if isinstance(value, bool) or isinstance(row_id, bool):
        raise InvalidRequest(_INVALID_CURSOR)

    return Cursor(
        value=_normalized(value, sort.value_type),
        id=_normalized(row_id, sort.id_type),
    )


def _normalized(value: Any, value_type: ValueType) -> CursorValue:
    """The value in the form its column takes, or invalid_cursor.

    TIMESTAMP and UUID are re-emitted canonically instead of passed through.
    Python's parsers are wider than Postgres's input syntax — it reads
    ISO-8601 week dates and the urn:uuid: form, Postgres reads neither — and
    the string in the cursor is the string that goes into the filter. So
    whatever Python accepted is sent in a form Postgres accepts too, which
    keeps a forged cursor a 422 instead of a 502 from a rejected query. The
    instant and the uuid are unchanged, so the comparison is the same one.
    """
    match value_type:
        case ValueType.TIMESTAMP:
            return _parsed(datetime.fromisoformat, value).isoformat()
        case ValueType.UUID:
            return str(_parsed(uuid.UUID, value))
        case ValueType.TEXT:
            if isinstance(value, str) and _is_transportable(value):
                return value
        case ValueType.INT:
            if isinstance(value, int) and _INT_MIN <= value <= _INT_MAX:
                return value
        case ValueType.FLOAT:
            if _is_finite_number(value):
                return value

    raise InvalidRequest(_INVALID_CURSOR)


def _parsed(parse, value: Any):
    if not isinstance(value, str):
        raise InvalidRequest(_INVALID_CURSOR)

    try:
        return parse(value)
    except ValueError as exc:
        raise InvalidRequest(_INVALID_CURSOR) from exc


def _is_transportable(value: str) -> bool:
    # TEXT is the only type that passes a string straight through, so it is
    # the only one that has to check the string can make the trip: into a
    # query string, back out as JSON, and into a Postgres text column. A lone
    # surrogate — which json.loads accepts — cannot be encoded at all, and
    # Postgres text cannot hold a NUL. Both would fail somewhere downstream
    # as an upstream error instead of here as the bad cursor they are.
    if "\x00" in value:
        return False

    try:
        value.encode()
    except UnicodeEncodeError:
        return False

    return True


def _is_finite_number(value: Any) -> bool:
    # json.loads reads the NaN, Infinity and -Infinity literals, so a
    # hand-built cursor can carry one. Every comparison against NaN is false
    # in SQL, which would answer an empty page instead of an error, and an
    # infinity is no row's sort key.
    if isinstance(value, float):
        return math.isfinite(value)

    # A whole number arrives from JSON as int whatever the column is, so a
    # float key accepts one — but ints are unbounded in Python, and one past
    # the widest double makes math.isfinite raise OverflowError. The range
    # check is what keeps that on the invalid_cursor path, and it never
    # converts: comparing an int to a float is exact in CPython.
    return isinstance(value, int) and -_FLOAT_MAX <= value <= _FLOAT_MAX


def page_params(
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
) -> PageRequest:
    """Route dependency. A limit outside 1..100 never gets here: FastAPI
    rejects it as invalid_request, the same path every other query param
    takes. The cursor is carried raw — only the service's SortKey can say
    whether it is valid."""
    # An empty cursor is the absent cursor: a client echoing a null one back
    # is asking for the first page, not sending a malformed one.
    return PageRequest(limit=limit, cursor=cursor or None)


_ESCAPES = str.maketrans({'"': '\\"', "\\": "\\\\"})


def _literal(value: CursorValue) -> str:
    # PostgREST reads , . ( ) as syntax inside an or= group, so anything that
    # is not a plain number is quoted: an unquoted title or timestamp would
    # split the filter apart.
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)

    return '"' + str(value).translate(_ESCAPES) + '"'


def keyset_filter(sort: SortKey, cursor: Cursor) -> str:
    """The composite condition, as a PostgREST or= payload: everything
    strictly past the cursor row, with the id breaking ties on equal keys."""
    op = "lt" if sort.descending else "gt"
    value = _literal(cursor.value)
    row_id = _literal(cursor.id)

    return (
        f"{sort.column}.{op}.{value},"
        f"and({sort.column}.eq.{value},{sort.id_column}.{op}.{row_id})"
    )


def apply_page(query: QueryT, sort: SortKey, request: PageRequest) -> QueryT:
    """Decodes the cursor against the sort key, then applies the filter, the
    ordering and the limit+1 probe to a supabase-py query, and returns it for
    the service to execute. A bad cursor raises before the query is built, so
    it is answered as invalid_cursor and never as an upstream failure."""
    cursor = request.decode(sort)

    if cursor is not None:
        query = query.or_(keyset_filter(sort, cursor))

    # The id has to be in the ORDER BY too: without it rows sharing a sort
    # key come back in an arbitrary order and the tiebreaker in the filter
    # means nothing.
    return (
        query.order(sort.column, desc=sort.descending)
        .order(sort.id_column, desc=sort.descending)
        .limit(request.fetch_limit)
    )


def build_page(
    rows: list[dict],
    request: PageRequest,
    sort: SortKey,
    total: int | None,
) -> tuple[list[dict], PageBlock]:
    """Splits the limit+1 rows into the page and its block. The extra row is
    dropped: it only ever existed to answer has_more.

    total is required and must line up with the request: exact on the first
    page, absent afterwards. Passing it the count the same request asked for
    (PageRequest.count_mode) always satisfies that; anything else is a bug
    the caller has to see, so it raises rather than quietly correcting the
    page block.

    Both refusals here — a total that does not match the request, and a row
    whose sort value could not be read back as a cursor — raise ValueError
    and not an AppError on purpose, so they answer 500 internal_error. The
    database answered correctly in each case; what is wrong is an invariant
    only this codebase's own writes could break, which is the definition of
    a bug and not of an upstream failure a client should retry. Settled, not
    an oversight: do not turn either into a 502."""
    if request.is_first_page and total is None:
        raise ValueError("first page needs an exact total")

    if not request.is_first_page and total is not None:
        raise ValueError("a cursored page must not carry a total")

    has_more = len(rows) > request.limit
    items = rows[: request.limit]

    # A row missing the sort or id column means the query selected the wrong
    # columns. The KeyError is left to translate_upstream_errors, which
    # reports it as upstream_error instead of a page that silently
    # cannot be continued.
    next_cursor = _next_cursor(items[-1], sort) if has_more else None

    return items, PageBlock(
        limit=request.limit,
        next_cursor=next_cursor,
        has_more=has_more,
        total=total,
    )


def _next_cursor(row: dict, sort: SortKey) -> str:
    # Emitted through the same normalizer that reads a cursor back, so what
    # goes out is what comes in. A row value that would not survive the round
    # trip cannot become a cursor the next request has to reject: it is
    # corrupt data on our side, so it raises here like the total misuse does
    # and is answered as internal_error.
    return encode_cursor(
        _row_value(row[sort.column], sort.value_type, sort.column),
        _row_value(row[sort.id_column], sort.id_type, sort.id_column),
    )


def _row_value(value: Any, value_type: ValueType, column: str) -> CursorValue:
    try:
        return _normalized(value, value_type)
    except InvalidRequest as exc:
        raise ValueError(f"column {column} cannot be a cursor value") from exc
