# INFO: Generates, stores and reads the personalized artist and album recommendations of a user.

# Imports other services, like public_service: it composes get_artist and
# get_album, the pages the recommendations are made of.

import contextvars
import logging
import re
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from itertools import zip_longest
from typing import Protocol, TypeVar

from supabase import Client

from core.cache import CacheClient
from core.config import settings
from core.exceptions import UpstreamError
from core.image_color_scope import detach_image_color_scope
from core.search_provider import (
    ProviderResourceMissing,
    SearchProvider,
    provider_raise_if_browse_missing,
)
from core.thumbnails import square_thumbnail_url
from core.upstream import translate_upstream_errors
from models.album import Album, AlbumRef
from models.artist import Artist, ArtistRef
from models.recommendations import (
    RecommendationReason,
    Recommendations,
    RecommendationSeed,
    RecommendedAlbum,
    RecommendedArtist,
    StoredRecommendation,
)
from services.album_service import get_album
from services.artist_service import get_artist

# setup_logging() is called once, in app.py, and is not called again here.
logger = logging.getLogger(__name__)

T = TypeVar("T")


class _HasId(Protocol):
    id: str


H = TypeVar("H", bound=_HasId)

# The quantities are decisions of the issue, not settings.
_BUCKET = "for_you"
_SOURCE = "similar_v1"
_ARTIST_COUNT = 15
_ALBUM_COUNT = 15
_MAX_ARTIST_SEEDS = 5
# PostgREST's row cap, the same bound as _TRACKS_LIMIT in playlist_service.
_FAVORITES_LIMIT = 1000
# An explicit cap, not pagination: it is PostgREST's max_rows. Past it the
# least played of the artists played often can be recommended, which is
# accepted.
_PLAYED_OFTEN_LIMIT = 1000
# The id patterns of routes/artist.py and routes/album.py: an id the routes
# would reject is not a seed, or get_artist/get_album would fail on it.
_ARTIST_ID = re.compile(r"^(MPLA)?UC")
_ALBUM_ID = re.compile(r"^MPRE")
_SEED_THUMBNAIL_SIZE = 544


def _empty() -> Recommendations:
    return Recommendations(artists=[], albums=[], generated_at=None, valid_until=None)


def get_recommendations(db: Client, user_id: str) -> Recommendations:
    with translate_upstream_errors():
        # get_db (service-role): the table has no privileges for the client
        # roles (migration 047), so the user_id filter is the only barrier.
        response = (
            db.table("user_recommendations")
            .select(
                "item_type, item_id, rank, seed_type, seed_id, seed_name, "
                "seed_thumb, payload, generated_at, valid_until"
            )
            .eq("user_id", user_id)
            .eq("bucket", _BUCKET)
            .order("item_type")
            .order("rank")
            .limit(_ARTIST_COUNT + _ALBUM_COUNT)
            .execute()
        )
        rows = [StoredRecommendation.model_validate(row) for row in response.data]
        if not rows:
            return _empty()

        artists: list[RecommendedArtist] = []
        albums: list[RecommendedAlbum] = []
        for row in rows:
            reason = RecommendationReason(
                seed=RecommendationSeed(
                    type=row.seed_type,
                    id=row.seed_id,
                    name=row.seed_name,
                    thumbnail_url=row.seed_thumb,
                )
            )
            if row.item_type == "artist":
                artists.append(
                    RecommendedArtist(
                        id=row.item_id,
                        name=row.payload["name"],
                        thumbnail_url=row.payload.get("thumbnail_url"),
                        reason=reason,
                    )
                )
            else:
                albums.append(
                    RecommendedAlbum(
                        id=row.item_id,
                        title=row.payload["title"],
                        artists=row.payload.get("artists") or [],
                        year=row.payload.get("year"),
                        audio_playlist_id=row.payload.get("audio_playlist_id"),
                        thumbnail_url=row.payload.get("thumbnail_url"),
                        reason=reason,
                    )
                )
        # All the rows come from one call of the replacing function, so they
        # share both timestamps.
        return Recommendations(
            artists=artists,
            albums=albums,
            generated_at=rows[0].generated_at,
            valid_until=rows[0].valid_until,
        )


def generate_recommendations(
    db: Client, provider: SearchProvider, cache: CacheClient, user_id: str
) -> Recommendations:
    current = get_recommendations(db, user_id)
    if current.valid_until is not None and datetime.fromisoformat(
        current.valid_until
    ) > datetime.now(UTC):
        return current

    now = datetime.now(UTC)
    favorites = _favorite_artist_ids(db, user_id)
    listened = _top_ids(
        db,
        "get_top_played_artists",
        "artist_id",
        user_id,
        now - timedelta(days=settings.recommendations_seed_window_days),
        _MAX_ARTIST_SEEDS + len(favorites),
        min_plays=1,
    )
    known = (
        _top_ids(
            db,
            "get_top_played_artists",
            "artist_id",
            user_id,
            now - timedelta(days=settings.recommendations_exclude_window_days),
            settings.recommendations_exclude_top_artists,
            min_plays=1,
        )
        if settings.recommendations_exclude_top_artists > 0
        else []
    )
    played_often = _top_ids(
        db,
        "get_top_played_artists",
        "artist_id",
        user_id,
        now - timedelta(days=settings.recommendations_exclude_window_days),
        _PLAYED_OFTEN_LIMIT,
        min_plays=settings.recommendations_exclude_min_plays,
    )
    history_album_ids = (
        _top_ids(
            db,
            "get_top_played_albums",
            "album_id",
            user_id,
            now - timedelta(days=settings.recommendations_seed_window_days),
            settings.recommendations_history_albums,
        )
        if settings.recommendations_history_albums > 0
        else []
    )
    plays = _count_plays_since(
        db,
        user_id,
        current.generated_at
        or (
            now - timedelta(days=settings.recommendations_seed_window_days)
        ).isoformat(),
    )

    artist_seed_ids = _artist_seed_ids(favorites, listened)
    album_seed_ids = [i for i in history_album_ids if _ALBUM_ID.match(i)]
    if not artist_seed_ids and not album_seed_ids:
        return _empty()

    # Stage 1: the pages of the artist seeds. A seed the provider confirms
    # is gone is skipped and the rounds spread over the rest.
    seed_pages = [
        page
        for page in _parallel(
            lambda i: _fetch_artist_or_none(provider, cache, i, "seed_artist"),
            artist_seed_ids,
        )
        if page is not None
    ]

    # Stage 2: the pages of the chosen similar artists.
    excluded = set(favorites) | set(known) | set(played_often) | set(artist_seed_ids)
    similar_pages, chosen = _choose_artists(provider, cache, seed_pages, excluded)

    # Stage 3: the album seeds of the play history.
    album_pages = [
        page
        for page in _parallel(
            lambda i: _fetch_album_or_none(provider, cache, i, "history_album"),
            album_seed_ids,
        )
        if page is not None
    ]

    if not seed_pages and not album_pages:
        return _empty()

    rows = _build_rows(seed_pages, similar_pages, chosen, album_pages, album_seed_ids)
    if not rows:
        return _empty()

    valid_until = now + timedelta(days=_validity_days(plays))
    with translate_upstream_errors():
        response = db.rpc(
            "replace_user_recommendations",
            {
                "p_user_id": user_id,
                "p_bucket": _BUCKET,
                "p_source": _SOURCE,
                "p_generated_at": now.isoformat(),
                "p_valid_until": valid_until.isoformat(),
                "p_rows": rows,
            },
        ).execute()
    inserted = response.data
    # A count that is not the rows sent is an anomaly, not a short success.
    if isinstance(inserted, bool) or inserted != len(rows):
        raise UpstreamError()

    return get_recommendations(db, user_id)


def _favorite_artist_ids(db: Client, user_id: str) -> list[str]:
    with translate_upstream_errors():
        response = (
            db.table("library_items")
            .select("external_id")
            .eq("user_id", user_id)
            .eq("kind", "artist")
            .order("added_at", desc=True)
            .limit(_FAVORITES_LIMIT)
            .execute()
        )
        return [row["external_id"] for row in response.data]


def _top_ids(
    db: Client,
    function: str,
    column: str,
    user_id: str,
    since: datetime,
    limit: int,
    *,
    min_plays: int | None = None,
) -> list[str]:
    params: dict[str, str | int] = {
        "p_user_id": user_id,
        "p_since": since.isoformat(),
        "p_limit": limit,
    }
    if min_plays is not None:
        params["p_min_plays"] = min_plays
    with translate_upstream_errors():
        response = db.rpc(function, params).execute()
        # A result that is not a list is an anomaly of the database, not an
        # empty history.
        if not isinstance(response.data, list):
            raise UpstreamError()
        return [row[column] for row in response.data]


def _count_plays_since(db: Client, user_id: str, since: str) -> int:
    with translate_upstream_errors():
        response = db.rpc(
            "count_plays_since", {"p_user_id": user_id, "p_since": since}
        ).execute()
    count = response.data
    if isinstance(count, bool) or not isinstance(count, int):
        raise UpstreamError()
    return count


def _validity_days(plays: int) -> int:
    if plays >= settings.recommendations_tier_high_plays:
        return settings.recommendations_tier_high_days
    if plays >= settings.recommendations_tier_mid_plays:
        return settings.recommendations_tier_mid_days
    return settings.recommendations_tier_low_days


def _artist_seed_ids(favorites: Sequence[str], listened: Sequence[str]) -> list[str]:
    # favorite[0], listened[0], favorite[1], ...: when one list runs out the
    # other goes on. Ids the artist route would reject are skipped.
    seeds: list[str] = []
    for pair in zip_longest(favorites, listened):
        for artist_id in pair:
            if (
                artist_id is not None
                and _ARTIST_ID.match(artist_id)
                and artist_id not in seeds
                and len(seeds) < _MAX_ARTIST_SEEDS
            ):
                seeds.append(artist_id)
    return seeds


def _rounds(
    sources: Sequence[Sequence[H]],
    limit: int,
    taken: set[str],
    *,
    max_rounds: int | None = None,
) -> list[tuple[int, H]]:
    # Round r: each source, in order, gives its next item whose id is not
    # taken yet. Stops at `limit`, at `max_rounds`, or when a round adds
    # nothing. Adds what it picks to `taken`. Items carry an `id`.
    picked: list[tuple[int, H]] = []
    cursors = [0] * len(sources)
    rounds = 0
    while len(picked) < limit and (max_rounds is None or rounds < max_rounds):
        progressed = False
        for index, source in enumerate(sources):
            if len(picked) >= limit:
                break
            while cursors[index] < len(source) and source[cursors[index]].id in taken:
                cursors[index] += 1
            if cursors[index] < len(source):
                item = source[cursors[index]]
                cursors[index] += 1
                taken.add(item.id)
                picked.append((index, item))
                progressed = True
        rounds += 1
        if not progressed:
            break
    return picked


def _choose_artists(
    provider: SearchProvider,
    cache: CacheClient,
    seed_pages: list[Artist],
    excluded: set[str],
) -> tuple[dict[str, Artist], list[tuple[int, ArtistRef]]]:
    # The pages of the chosen artists are fetched (a page is also the proof
    # the artist exists); one the provider confirms is gone is excluded and
    # its seed takes its next candidate, and only the new ones are fetched.
    # Each id is fetched once, so the loop ends with the candidates.
    pages: dict[str, Artist] = {}
    gone: set[str] = set()
    candidates = [
        [r for r in seed.related if _ARTIST_ID.match(r.id)] for seed in seed_pages
    ]
    while True:
        chosen = _rounds(candidates, _ARTIST_COUNT, set(excluded) | gone)
        pending = [ref.id for _, ref in chosen if ref.id not in pages]
        if not pending:
            return pages, chosen
        fetched = _parallel(
            lambda i: _fetch_artist_or_none(provider, cache, i, "similar_artist"),
            pending,
        )
        for artist_id, page in zip(pending, fetched, strict=True):
            if page is None:
                gone.add(artist_id)
            else:
                pages[artist_id] = page


def _seed_of_artist(page: Artist) -> RecommendationSeed:
    return RecommendationSeed(
        type="artist",
        id=page.id,
        name=page.name,
        thumbnail_url=square_thumbnail_url(
            page.thumbnail_url, _SEED_THUMBNAIL_SIZE, smart_crop=True
        ),
    )


def _seed_of_album(page: Album) -> RecommendationSeed:
    return RecommendationSeed(
        type="album", id=page.id, name=page.title, thumbnail_url=page.thumbnail_url
    )


def _interleave(lists: Sequence[Sequence[T]]) -> list[T]:
    return [item for group in zip_longest(*lists) for item in group if item is not None]


def _build_rows(
    seed_pages: list[Artist],
    similar_pages: dict[str, Artist],
    chosen: list[tuple[int, ArtistRef]],
    album_pages: list[Album],
    album_seed_ids: list[str],
) -> list[dict]:
    artist_seeds = [_seed_of_artist(page) for page in seed_pages]
    album_seeds = [_seed_of_album(page) for page in album_pages]

    # The albums of an artist seed come from its chosen similars, one album
    # of each in turn.
    similars_of_seed: list[list[Artist]] = [[] for _ in seed_pages]
    for index, ref in chosen:
        similars_of_seed[index].append(similar_pages[ref.id])
    from_artists = [
        _interleave([similar.albums for similar in similars])
        for similars in similars_of_seed
    ]
    from_albums = [page.related_recommendations for page in album_pages]

    # Every album seed is excluded, whether or not its page was found.
    taken = set(album_seed_ids)
    reserve = _rounds(
        from_albums, settings.recommendations_history_albums, taken, max_rounds=1
    )
    main = _rounds(from_artists, _ALBUM_COUNT - len(reserve), taken)
    extra = _rounds(from_albums, _ALBUM_COUNT - len(reserve) - len(main), taken)

    albums: list[tuple[AlbumRef, RecommendationSeed]] = [
        *((ref, artist_seeds[i]) for i, ref in main),
        *((ref, album_seeds[i]) for i, ref in reserve),
        *((ref, album_seeds[i]) for i, ref in extra),
    ]

    rows: list[dict] = []
    for rank, (index, ref) in enumerate(chosen, start=1):
        rows.append(
            _row(
                "artist",
                ref.id,
                rank,
                artist_seeds[index],
                ref.model_dump(include={"name", "thumbnail_url"}),
            )
        )
    for rank, (ref, seed) in enumerate(albums, start=1):
        # include, not model_dump(): image is a computed field and the
        # tiers are never stored (ADR 019).
        rows.append(
            _row(
                "album",
                ref.id,
                rank,
                seed,
                ref.model_dump(
                    include={
                        "title",
                        "thumbnail_url",
                        "artists",
                        "year",
                        "audio_playlist_id",
                    }
                ),
            )
        )
    return rows


def _row(
    item_type: str, item_id: str, rank: int, seed: RecommendationSeed, payload: dict
) -> dict:
    return {
        "item_type": item_type,
        "item_id": item_id,
        "rank": rank,
        "seed_type": seed.type,
        "seed_id": seed.id,
        "seed_name": seed.name,
        "seed_thumb": seed.thumbnail_url,
        "payload": payload,
    }


def _fetch_artist_or_none(
    provider: SearchProvider, cache: CacheClient, artist_id: str, kind: str
) -> Artist | None:
    # None only when the provider confirmed, in a structured field, that the
    # artist does not exist (provider_raise_if_browse_missing): an empty with
    # proof, like provider_get_song_credits. get_artist keeps answering 502
    # for the same id on /artist (ADR 002). Anything else, a timeout
    # included, goes through.
    try:
        artist, _ = get_artist(provider, cache, artist_id)
        return artist
    except UpstreamError as exc:
        _skip_if_confirmed_missing(provider, artist_id, kind, exc)
        return None


def _fetch_album_or_none(
    provider: SearchProvider, cache: CacheClient, album_id: str, kind: str
) -> Album | None:
    # Same rule as _fetch_artist_or_none, for an album page.
    try:
        album, _ = get_album(provider, cache, album_id)
        return album
    except UpstreamError as exc:
        _skip_if_confirmed_missing(provider, album_id, kind, exc)
        return None


def _skip_if_confirmed_missing(
    provider: SearchProvider, browse_id: str, kind: str, exc: UpstreamError
) -> None:
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
        logger.info(
            "recommendations skipped %s %s: %s", kind, browse_id, type(cause).__name__
        )
        return
    raise exc


def _parallel(fn: Callable[[T], object], items: Sequence[T]) -> list:
    # One executor per generation (a per-process pool would queue the
    # generations of different users inside each other's 12 s budget). Each
    # task runs in its own copy of the request's context, so the provider
    # budget applies; Context.run does not allow two threads in one Context.
    if not items:
        return []
    with ThreadPoolExecutor(
        max_workers=settings.recommendations_parallel_calls
    ) as pool:
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
    # The models a worker builds and caches are not the response: their
    # images must not be noted in the request's color scope.
    detach_image_color_scope()
    return fn(item)
