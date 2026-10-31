# INFO: Reads the curated artists of the onboarding, picks similar artists and closes the onboarding of a user.

# Imports artist_service, like recommendations_service: the page of an artist
# the user picked from the search is get_artist, and a page is also the proof
# the artist exists.

import contextvars
import logging
import re
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import TypeVar

from supabase import Client

from core.cache import CacheClient
from core.exceptions import NotFound, TooFewArtists, UpstreamError
from core.image_color_scope import detach_image_color_scope
from core.pagination import PageRequest, SortKey, ValueType, apply_page, build_page
from core.search_provider import (
    ProviderResourceMissing,
    SearchProvider,
    provider_raise_if_browse_missing,
)
from core.upstream import translate_upstream_errors
from models.artist import Artist
from models.onboarding import (
    MIN_ARTIST_IDS,
    OnboardingArtist,
    OnboardingCompletion,
    SimilarArtists,
)
from models.profiles import Profile
from models.responses import PageBlock
from services.artist_service import get_artist

# setup_logging() is called once, in app.py, and is not called again here.
logger = logging.getLogger(__name__)

T = TypeVar("T")

_SORT = SortKey(
    "position",
    ValueType.INT,
    descending=False,
    id_column="id",
    id_type=ValueType.TEXT,
)
# position is not exposed: it is the order of the list, and the cursor needs
# it on every row.
_CURATED_COLUMNS = "id, name, thumbnail_url, position"

# Provider calls at a time. The measured default of ADR 025, a constant of
# this module and not a setting.
_PARALLEL_CALLS = 4

_SIMILAR_COUNT = 3
# Copy of the same constant in services/recommendations_service.py; keep both
# in step.
_ARTIST_ID = re.compile(r"^(MPLA)?UC")

# library.md fixes source "external" for a saved artist.
_KIND = "artist"
_SOURCE = "external"


def list_onboarding_artists(
    db: Client, page: PageRequest
) -> tuple[list[OnboardingArtist], PageBlock]:
    with translate_upstream_errors():
        # get_db (service-role): the table has no privileges for the client
        # roles (migration 049).
        query = db.table("onboarding_artists").select(
            _CURATED_COLUMNS, count=page.count_mode
        )
        response = apply_page(query, _SORT, page).execute()
        rows, page_block = build_page(response.data or [], page, _SORT, response.count)
        return [OnboardingArtist(**row) for row in rows], page_block


def complete_onboarding(
    user_db: Client,
    db: Client,
    provider: SearchProvider,
    cache: CacheClient,
    profile: Profile,
    artist_ids: list[str],
) -> OnboardingCompletion:
    curated = _curated_rows(db, artist_ids)

    # Only the ids outside the curated table go to the provider (the ones
    # the user found in the search), in parallel.
    pending = [i for i in artist_ids if i not in curated]
    pages = dict(
        zip(
            pending,
            _parallel(lambda i: _fetch_artist_or_none(provider, cache, i), pending),
            strict=True,
        )
    )
    skipped = [i for i in pending if pages[i] is None]

    valid: list[tuple[str, str, str | None]] = []
    for artist_id in artist_ids:
        if artist_id in curated:
            row = curated[artist_id]
            valid.append((artist_id, row["name"], row["thumbnail_url"]))
        elif (page := pages[artist_id]) is not None:
            valid.append((artist_id, page.name, page.thumbnail_url))
    if len(valid) < MIN_ARTIST_IDS:
        raise TooFewArtists()

    _save_library(user_db, profile.id, valid)
    completed_at = _mark_profile(db, profile)
    return OnboardingCompletion(
        onboarding_completed_at=completed_at, saved=len(valid), skipped=skipped
    )


def similar_artists(
    provider: SearchProvider,
    cache: CacheClient,
    artist_id: str,
    exclude_ids: list[str],
) -> SimilarArtists:
    # The page runs in a worker so that its images are not noted in the
    # request's color scope: only the items returned are.
    (page,) = _parallel(
        lambda i: _fetch_artist_or_none(provider, cache, i), [artist_id]
    )
    if page is None:
        return SimilarArtists(artists=[])
    taken = {_bare(artist_id), *(_bare(i) for i in exclude_ids)}
    chosen: list[OnboardingArtist] = []
    for related in page.related:
        if len(chosen) == _SIMILAR_COUNT:
            break
        if not _ARTIST_ID.match(related.id) or _bare(related.id) in taken:
            continue
        taken.add(_bare(related.id))
        chosen.append(
            OnboardingArtist(
                id=related.id,
                name=related.name,
                thumbnail_url=related.thumbnail_url,
            )
        )
    return SimilarArtists(artists=chosen)


def _bare(artist_id: str) -> str:
    return artist_id.removeprefix("MPLA")


def _curated_rows(db: Client, artist_ids: list[str]) -> dict[str, dict]:
    with translate_upstream_errors():
        response = (
            db.table("onboarding_artists")
            .select("id, name, thumbnail_url")
            .in_("id", artist_ids)
            .execute()
        )
        return {row["id"]: row for row in response.data}


def _save_library(
    user_db: Client, user_id: str, valid: list[tuple[str, str, str | None]]
) -> None:
    with_photo = []
    without_photo = []
    for artist_id, name, thumbnail_url in valid:
        row = {
            "user_id": user_id,
            "kind": _KIND,
            "external_id": artist_id,
            "title": name,
            "source": _SOURCE,
        }
        if thumbnail_url is None:
            without_photo.append(row)
        else:
            row["thumbnail_url"] = thumbnail_url
            with_photo.append(row)
    # Two upserts, because a bulk upsert sends the union of the keys and a
    # row missing one of them gets NULL, which thumbnail_url (NOT NULL) rejects.
    # The rows without a photo go in their own upsert with no thumbnail_url
    # column at all: a new row takes the DEFAULT (''), and an artist already in
    # the library keeps the photo it has. Not atomic between the two calls; a
    # retry does not duplicate (same on_conflict).
    with translate_upstream_errors():
        for rows in (with_photo, without_photo):
            if not rows:
                continue
            response = (
                user_db.table("library_items")
                .upsert(rows, on_conflict="user_id,kind,external_id")
                .execute()
            )
            # A count that is not the rows sent is an anomaly, not a short
            # success.
            if len(response.data or []) != len(rows):
                raise UpstreamError()


def _mark_profile(db: Client, profile: Profile) -> str:
    # A second close keeps the first instant.
    if profile.onboarding_completed_at is not None:
        return profile.onboarding_completed_at
    with translate_upstream_errors():
        # get_db (service-role): migration 050 takes the UPDATE of this
        # column from authenticated, so the filter by the id of the token is
        # the only barrier. The clock is Python's, like generated_at in
        # recommendations.
        response = (
            db.table("profiles")
            .update({"onboarding_completed_at": datetime.now(UTC).isoformat()})
            .eq("id", profile.id)
            .is_("onboarding_completed_at", "null")
            .execute()
        )
        if not response.data:
            # Another request marked it in between: read what it wrote.
            response = (
                db.table("profiles")
                .select("onboarding_completed_at")
                .eq("id", profile.id)
                .execute()
            )
        if not response.data:
            raise NotFound("profile_not_found")
        completed_at = response.data[0]["onboarding_completed_at"]
        if completed_at is None:
            raise UpstreamError()
        return completed_at


def _fetch_artist_or_none(
    provider: SearchProvider, cache: CacheClient, artist_id: str
) -> Artist | None:
    # Copy of the same helper in services/recommendations_service.py; keep
    # both in step.
    # None only when the provider confirmed, in a structured field, that the
    # artist does not exist (provider_raise_if_browse_missing): an empty with
    # proof, like provider_get_song_credits. get_artist keeps answering 502
    # for the same id on /artist (ADR 002). Anything else, a timeout
    # included, goes through.
    try:
        artist, _ = get_artist(provider, cache, artist_id)
        return artist
    except UpstreamError as exc:
        _skip_if_confirmed_missing(provider, artist_id, exc)
        return None


def _skip_if_confirmed_missing(
    provider: SearchProvider, browse_id: str, exc: UpstreamError
) -> None:
    # Copy of the same helper in services/recommendations_service.py; keep
    # both in step.
    # Returns only when the id is confirmed missing; otherwise it re-raises
    # the original failure. A missing id shows up as a KeyError of the
    # library's navigation (measured); a server error, a parse error or a
    # network failure is not probed. UpstreamTimeout is not an UpstreamError,
    # so it never gets here.
    cause = exc.__cause__
    if not isinstance(cause, KeyError):
        raise exc
    try:
        with translate_upstream_errors():
            provider_raise_if_browse_missing(provider, browse_id, cause)
    except ProviderResourceMissing:
        logger.info("onboarding skipped artist %s: %s", browse_id, type(cause).__name__)
        return
    raise exc


def _parallel(fn: Callable[[T], object], items: Sequence[T]) -> list:
    # Copy of the same helper in services/recommendations_service.py; keep
    # both in step.
    # One executor per request (a per-process pool would queue the requests
    # of different users inside each other's 12 s budget). Each task runs in
    # its own copy of the request's context, so the provider budget applies;
    # Context.run does not allow two threads in one Context.
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=_PARALLEL_CALLS) as pool:
        futures = [
            pool.submit(contextvars.copy_context().run, _in_worker, fn, item)
            for item in items
        ]
        try:
            return [future.result() for future in futures]
        finally:
            # After the first exception the pending calls do not start; the
            # ones already running end bounded by the budget.
            for future in futures:
                future.cancel()


def _in_worker(fn: Callable[[T], object], item: T) -> object:
    # Copy of the same helper in services/recommendations_service.py; keep
    # both in step.
    # The models a worker builds and caches are not the response: their
    # images must not be noted in the request's color scope.
    detach_image_color_scope()
    return fn(item)
