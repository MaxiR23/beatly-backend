# INFO: Generates, stores and reads the personalized artist and album recommendations of a user.

# Imports other services, like public_service: it composes get_artist and
# get_album, the pages the recommendations are made of.

import contextvars
import logging
import re
from collections.abc import Callable, Hashable, Iterable, Sequence
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
    SeedSection,
    SeedSectionAlbum,
    SeedSectionArtist,
    StoredPlacement,
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
_ALBUMS_FOR_YOU = 10
_SEED_SECTIONS = 3
_SECTION_ARTISTS = 3
_SECTION_ALBUMS = 3
_RECOMMENDED_ALBUMS = 10
_RECOMMENDED_ARTISTS = 10
_ARTIST_COUNT = _SEED_SECTIONS * _SECTION_ARTISTS + _RECOMMENDED_ARTISTS
_ALBUM_COUNT = _ALBUMS_FOR_YOU + _SEED_SECTIONS * _SECTION_ALBUMS + _RECOMMENDED_ALBUMS
# Keys of the payload that say where a row sits in the response (ADR 026).
_SECTION_KEY = "section"
_SECTION_INDEX_KEY = "section_index"
_ALBUMS_FOR_YOU_SECTION = "albums_for_you"
_SEED_SECTIONS_SECTION = "seed_sections"
_RECOMMENDED_ALBUMS_SECTION = "recommended_albums"
_RECOMMENDED_ARTISTS_SECTION = "recommended_artists"
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
# Editions of one album (#227): the trailing groups in parentheses or square
# brackets, not nested, are an edition suffix ("(Deluxe)", "(Epilogue)").
_EDITION_SUFFIX = re.compile(r"\s*(?:\([^()]*\)|\[[^\[\]]*\])\s*$")
# Soundtracks are not recommended (#227): "Soundtrack" or "OST" as a whole
# word, in any case. Whole word so that "Ghost" or "Lost" are not "OST".
_SOUNDTRACK = re.compile(r"\b(?:soundtrack|ost)\b", re.IGNORECASE)
_SEED_THUMBNAIL_SIZE = 544


def _base_title(title: str) -> str:
    base = title
    while True:
        stripped = _EDITION_SUFFIX.sub("", base)
        if stripped == base:
            break
        base = stripped
    return " ".join(base.split()).lower() or " ".join(title.split()).lower()


def _is_soundtrack(title: str) -> bool:
    return _SOUNDTRACK.search(title) is not None


def _edition_keys(title: str, artist_ids: Iterable[str | None]) -> set[tuple[str, str]]:
    # (artist, base title): the artist id without the MPLA prefix so the same
    # person matches across sources.
    return {
        (artist_id.removeprefix("MPLA"), _base_title(title))
        for artist_id in artist_ids
        if artist_id
    }


def _empty() -> Recommendations:
    return Recommendations(
        albums_for_you=[],
        seed_sections=[],
        recommended_albums=[],
        recommended_artists=[],
        artists=[],
        albums=[],
        generated_at=None,
        valid_until=None,
    )


def get_recommendations(db: Client, user_id: str) -> Recommendations:
    return _stored_set(db, user_id)[0]


def _reason_of(row: StoredRecommendation) -> RecommendationReason:
    return RecommendationReason(
        seed=RecommendationSeed(
            type=row.seed_type,
            id=row.seed_id,
            name=row.seed_name,
            thumbnail_url=row.seed_thumb,
        )
    )


def _artist_of(row: StoredRecommendation) -> RecommendedArtist:
    return RecommendedArtist(
        id=row.item_id,
        name=row.payload["name"],
        thumbnail_url=row.payload.get("thumbnail_url"),
        reason=_reason_of(row),
    )


def _album_of(row: StoredRecommendation) -> RecommendedAlbum:
    return RecommendedAlbum(
        id=row.item_id,
        title=row.payload["title"],
        artists=row.payload.get("artists") or [],
        year=row.payload.get("year"),
        audio_playlist_id=row.payload.get("audio_playlist_id"),
        thumbnail_url=row.payload.get("thumbnail_url"),
        reason=_reason_of(row),
    )


def _stored_set(db: Client, user_id: str) -> tuple[Recommendations, bool]:
    # The bool says whether the set has sections; a set stored before them
    # (#227) does not, and counts as expired for the generation.
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
            return _empty(), True

        artists = [_artist_of(r) for r in rows if r.item_type == "artist"]
        albums = [_album_of(r) for r in rows if r.item_type == "album"]
        # All the rows come from one call of the replacing function, so they
        # share both timestamps.
        generated_at = rows[0].generated_at
        valid_until = rows[0].valid_until

        if any(_SECTION_KEY not in row.payload for row in rows):
            return (
                Recommendations(
                    albums_for_you=[],
                    seed_sections=[],
                    recommended_albums=[],
                    recommended_artists=[],
                    artists=artists,
                    albums=albums,
                    generated_at=generated_at,
                    valid_until=valid_until,
                ),
                False,
            )

        albums_for_you: list[RecommendedAlbum] = []
        recommended_albums: list[RecommendedAlbum] = []
        recommended_artists: list[RecommendedArtist] = []
        # section_index -> (seed, artists, albums), each by rank.
        grouped: dict[
            int,
            tuple[
                RecommendationSeed,
                list[SeedSectionArtist],
                list[SeedSectionAlbum],
            ],
        ] = {}
        for row in rows:
            placement = StoredPlacement.model_validate(row.payload)
            section = placement.section
            # An item of the wrong type for its section is an anomaly of the
            # database, not an empty list.
            if (
                section in (_ALBUMS_FOR_YOU_SECTION, _RECOMMENDED_ALBUMS_SECTION)
                and row.item_type != "album"
            ) or (
                section == _RECOMMENDED_ARTISTS_SECTION and row.item_type != "artist"
            ):
                raise UpstreamError()
            if section == _ALBUMS_FOR_YOU_SECTION:
                albums_for_you.append(_album_of(row))
            elif section == _RECOMMENDED_ALBUMS_SECTION:
                recommended_albums.append(_album_of(row))
            elif section == _RECOMMENDED_ARTISTS_SECTION:
                recommended_artists.append(_artist_of(row))
            else:
                index = placement.section_index
                # StoredPlacement guarantees it; a row without it is an
                # anomaly of the database.
                if index is None:
                    raise UpstreamError()
                if index not in grouped:
                    grouped[index] = (_reason_of(row).seed, [], [])
                _, section_artists, section_albums = grouped[index]
                if row.item_type == "artist":
                    section_artists.append(
                        SeedSectionArtist(**_artist_of(row).model_dump())
                    )
                else:
                    section_albums.append(
                        SeedSectionAlbum(**_album_of(row).model_dump())
                    )

        seed_sections = [
            SeedSection(seed=seed, items=_interleave([sec_artists, sec_albums]))
            for _, (seed, sec_artists, sec_albums) in sorted(grouped.items())
        ]
        return (
            Recommendations(
                albums_for_you=albums_for_you,
                seed_sections=seed_sections,
                recommended_albums=recommended_albums,
                recommended_artists=recommended_artists,
                artists=artists,
                albums=albums,
                generated_at=generated_at,
                valid_until=valid_until,
            ),
            True,
        )


def generate_recommendations(
    db: Client, provider: SearchProvider, cache: CacheClient, user_id: str
) -> Recommendations:
    current, sectioned = _stored_set(db, user_id)
    if (
        sectioned
        and current.valid_until is not None
        and datetime.fromisoformat(current.valid_until) > datetime.now(UTC)
    ):
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
    # Only the similars that fit a list are fetched: 3 for each of the first
    # seed sections and the recommended artists. The limit is the most they
    # can be; _choose_artists drops the ones a short section leaves over.
    similar_pages, chosen = _choose_artists(
        provider,
        cache,
        seed_pages,
        excluded,
        min(len(seed_pages), _SEED_SECTIONS) * _SECTION_ARTISTS + _RECOMMENDED_ARTISTS,
    )

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
    taken: set[Hashable],
    *,
    max_rounds: int | None = None,
    keys: Callable[[H], Iterable[Hashable]] | None = None,
) -> list[tuple[int, H]]:
    # Round r: each source, in order, gives its next item none of whose keys
    # is taken yet (by default its id). Stops at `limit`, at `max_rounds`, or
    # when a round adds nothing. Adds the keys of what it picks to `taken`.
    # Items carry an `id`.
    def keys_of(item: H) -> Iterable[Hashable]:
        return (item.id,) if keys is None else keys(item)

    picked: list[tuple[int, H]] = []
    cursors = [0] * len(sources)
    rounds = 0
    while len(picked) < limit and (max_rounds is None or rounds < max_rounds):
        progressed = False
        for index, source in enumerate(sources):
            if len(picked) >= limit:
                break
            while cursors[index] < len(source) and any(
                key in taken for key in keys_of(source[cursors[index]])
            ):
                cursors[index] += 1
            if cursors[index] < len(source):
                item = source[cursors[index]]
                cursors[index] += 1
                taken.update(keys_of(item))
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
    limit: int,
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
        chosen = _fit_lists(
            _rounds(candidates, limit, set(excluded) | gone),
            min(len(seed_pages), _SEED_SECTIONS),
        )
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


def _fit_lists(
    chosen: list[tuple[int, ArtistRef]], sections: int
) -> list[tuple[int, ArtistRef]]:
    # What _build_rows places: the first 3 of each of the first `sections`
    # seeds go to its section, and of the rest (in order) 10 to the
    # recommended artists. A seed with less than 3 leaves cupos that the
    # rounds gave to others, so more than fit were chosen; the rest is cut
    # here, before any page is fetched.
    in_section = [0] * sections
    overflow = 0
    fitted: list[tuple[int, ArtistRef]] = []
    for index, ref in chosen:
        if index < sections and in_section[index] < _SECTION_ARTISTS:
            in_section[index] += 1
        elif overflow < _RECOMMENDED_ARTISTS:
            overflow += 1
        else:
            continue
        fitted.append((index, ref))
    return fitted


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


def _ranked_albums(
    from_artists: Sequence[Sequence[AlbumRef]],
    from_albums: Sequence[Sequence[AlbumRef]],
    artist_seeds: list[RecommendationSeed],
    album_seeds: list[RecommendationSeed],
    count: int,
    taken: set[Hashable],
    *,
    keys: Callable[[AlbumRef], Iterable[Hashable]] | None = None,
) -> list[tuple[AlbumRef, RecommendationSeed]]:
    # The reserve of the album seeds never exceeds the list it goes in.
    reserve = _rounds(
        from_albums,
        min(settings.recommendations_history_albums, count),
        taken,
        max_rounds=1,
        keys=keys,
    )
    main = _rounds(from_artists, count - len(reserve), taken, keys=keys)
    extra = _rounds(from_albums, count - len(reserve) - len(main), taken, keys=keys)
    return [
        *((ref, artist_seeds[i]) for i, ref in main),
        *((ref, album_seeds[i]) for i, ref in reserve),
        *((ref, album_seeds[i]) for i, ref in extra),
    ]


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
    refs_of_seed: list[list[ArtistRef]] = [[] for _ in seed_pages]
    similars_of_seed: list[list[Artist]] = [[] for _ in seed_pages]
    for index, ref in chosen:
        refs_of_seed[index].append(ref)
        similars_of_seed[index].append(similar_pages[ref.id])
    # Soundtracks go out of both sources before the split, so the next
    # candidate takes their place in every list.
    from_artists = [
        _interleave(
            [
                [ref for ref in similar.albums if not _is_soundtrack(ref.title)]
                for similar in similars
            ]
        )
        for similars in similars_of_seed
    ]
    from_albums = [
        [ref for ref in page.related_recommendations if not _is_soundtrack(ref.title)]
        for page in album_pages
    ]

    # The edition keys (artist, base title) of every album id, from every
    # source it appears in. The artist is each linked artist of the album
    # plus, for an album of an artist page, the owner of that page: the
    # linked artists can be missing there.
    editions: dict[str, set[tuple[str, str]]] = {}
    for similars in similars_of_seed:
        for similar in similars:
            for ref in similar.albums:
                editions.setdefault(ref.id, set()).update(
                    _edition_keys(ref.title, [*(a.id for a in ref.artists), similar.id])
                )
    for page in album_pages:
        for ref in page.related_recommendations:
            editions.setdefault(ref.id, set()).update(
                _edition_keys(ref.title, (a.id for a in ref.artists))
            )

    def album_keys(ref: AlbumRef) -> Iterable[Hashable]:
        return (ref.id, *editions.get(ref.id, ()))

    # One `taken` for every list: an album goes to the first list that asks
    # for it, in the order of the screen, and so does the first edition of an
    # album (same artist, same base title): the other one is skipped. Every
    # album seed is excluded, whether or not its page was found, and so are
    # the other editions of the seeds whose page was found.
    taken: set[Hashable] = set(album_seed_ids)
    for page in album_pages:
        taken.update(_edition_keys(page.title, (a.id for a in page.artists)))
    albums_for_you = _ranked_albums(
        from_artists,
        from_albums,
        artist_seeds,
        album_seeds,
        _ALBUMS_FOR_YOU,
        taken,
        keys=album_keys,
    )

    sections: list[
        tuple[int, list[ArtistRef], list[tuple[AlbumRef, RecommendationSeed]]]
    ] = []
    for i in range(min(len(seed_pages), _SEED_SECTIONS)):
        section_artists = refs_of_seed[i][:_SECTION_ARTISTS]
        section_albums = [
            (ref, artist_seeds[i])
            for _, ref in _rounds(
                [from_artists[i]], _SECTION_ALBUMS, taken, keys=album_keys
            )
        ]
        # A section with nothing is left out, and one with part is not filled
        # with the other type.
        if section_artists or section_albums:
            sections.append((i, section_artists, section_albums))

    recommended_albums = _ranked_albums(
        from_artists,
        from_albums,
        artist_seeds,
        album_seeds,
        _RECOMMENDED_ALBUMS,
        taken,
        keys=album_keys,
    )
    in_sections = {ref.id for _, refs, _ in sections for ref in refs}
    recommended_artists = [(i, ref) for i, ref in chosen if ref.id not in in_sections][
        :_RECOMMENDED_ARTISTS
    ]

    rows: list[dict] = []
    artist_rank = 0
    for i, refs, _ in sections:
        for ref in refs:
            artist_rank += 1
            rows.append(
                _artist_row(
                    ref,
                    artist_rank,
                    artist_seeds[i],
                    {_SECTION_KEY: _SEED_SECTIONS_SECTION, _SECTION_INDEX_KEY: i},
                )
            )
    for i, ref in recommended_artists:
        artist_rank += 1
        rows.append(
            _artist_row(
                ref,
                artist_rank,
                artist_seeds[i],
                {_SECTION_KEY: _RECOMMENDED_ARTISTS_SECTION},
            )
        )

    placed_albums: list[tuple[AlbumRef, RecommendationSeed, dict]] = [
        *(
            (ref, seed, {_SECTION_KEY: _ALBUMS_FOR_YOU_SECTION})
            for ref, seed in albums_for_you
        )
    ]
    for i, _, section_albums in sections:
        placed_albums.extend(
            (
                ref,
                seed,
                {_SECTION_KEY: _SEED_SECTIONS_SECTION, _SECTION_INDEX_KEY: i},
            )
            for ref, seed in section_albums
        )
    placed_albums.extend(
        (ref, seed, {_SECTION_KEY: _RECOMMENDED_ALBUMS_SECTION})
        for ref, seed in recommended_albums
    )
    for album_rank, (ref, seed, placement) in enumerate(placed_albums, start=1):
        # include, not model_dump(): image is a computed field and the
        # tiers are never stored (ADR 019).
        rows.append(
            _row(
                "album",
                ref.id,
                album_rank,
                seed,
                ref.model_dump(
                    include={
                        "title",
                        "thumbnail_url",
                        "artists",
                        "year",
                        "audio_playlist_id",
                    }
                )
                | placement,
            )
        )
    return rows


def _artist_row(
    ref: ArtistRef, rank: int, seed: RecommendationSeed, placement: dict
) -> dict:
    return _row(
        "artist",
        ref.id,
        rank,
        seed,
        ref.model_dump(include={"name", "thumbnail_url"}) | placement,
    )


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


# Copied in services/onboarding_service.py; keep both in step.
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


# Copied in services/onboarding_service.py; keep both in step.
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


# Copied in services/onboarding_service.py; keep both in step.
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


# Copied in services/onboarding_service.py; keep both in step.
def _in_worker(fn: Callable[[T], object], item: T) -> object:
    # The models a worker builds and caches are not the response: their
    # images must not be noted in the request's color scope.
    detach_image_color_scope()
    return fn(item)
