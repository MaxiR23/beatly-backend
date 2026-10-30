# INFO: Seeds public.onboarding_artists from a file of artist names (run by the repo owner).

# Run only by the repo owner, by hand, never by an agent and never from the
# app. Dry run by default: it searches every name and prints the artist each
# one resolves to, so the homonyms can be reviewed, and writes nothing.
# --write replaces the whole table with the matches, atomically, through
# replace_onboarding_artists (migration 049).
#
#     python -m scripts.seed_onboarding_artists [--write] [--names PATH]
#
# It uses the credentials of the Settings of the owner's environment (the
# service-role database client and the provider session); it does not read
# any credential file by itself. The writing does not know where the list
# came from: to change the source (a chart instead of a file) is to change
# resolve(), not the app.

import argparse
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from supabase import Client

from core.database import get_supabase
from core.exceptions import AppError, UpstreamError
from core.search_provider import SearchProvider, get_search_provider
from core.upstream import translate_upstream_errors
from models.search import SearchArtist
from services.search_service import find_artist

# setup_logging() belongs to app.py; this process configures logging below,
# in the __main__ block only.
logger = logging.getLogger(__name__)

NAMES_FILE = Path(__file__).with_name("onboarding_artists.txt")


@dataclass(frozen=True)
class Match:
    name: str
    artist: SearchArtist | None


def read_names(path: Path) -> list[str]:
    names = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(set(names)) != len(names):
        raise ValueError("the names file repeats a name")
    return names


def resolve(provider: SearchProvider, names: Sequence[str]) -> list[Match]:
    return [Match(name, find_artist(provider, name)) for name in names]


def rows_for(matches: Sequence[Match]) -> list[dict]:
    found = [m.artist for m in matches if m.artist is not None]
    ids = [artist.id for artist in found]
    if len(set(ids)) != len(ids):
        raise ValueError("two names resolve to the same artist")
    return [
        {
            "id": artist.id,
            "name": artist.name,
            "thumbnail_url": artist.thumbnail_url,
            "position": position,
        }
        for position, artist in enumerate(found, start=1)
    ]


def report(matches: Sequence[Match], out: TextIO) -> None:
    for match in matches:
        if match.artist is None:
            out.write(f"{match.name} -> no match\n")
        else:
            out.write(f"{match.name} -> {match.artist.id} ({match.artist.name})\n")
    found = sum(1 for m in matches if m.artist is not None)
    out.write(f"{found} of {len(matches)} matched\n")


def write(db: Client, rows: list[dict]) -> int:
    with translate_upstream_errors():
        response = db.rpc("replace_onboarding_artists", {"p_rows": rows}).execute()
    inserted = response.data
    # A count that is not the rows sent is an anomaly, not a short success.
    if isinstance(inserted, bool) or inserted != len(rows):
        raise UpstreamError()
    return inserted


def main(
    argv: Sequence[str] | None = None,
    *,
    provider: SearchProvider | None = None,
    db: Client | None = None,
    out: TextIO = sys.stdout,
) -> int:
    parser = argparse.ArgumentParser(description="Seed onboarding_artists.")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--names", type=Path, default=NAMES_FILE)
    args = parser.parse_args(argv)

    try:
        names = read_names(args.names)
        matches = resolve(provider or get_search_provider(), names)
        report(matches, out)
        if not args.write:
            return 0
        rows = rows_for(matches)
        if not rows:
            logger.error("nothing to write: no name matched")
            return 1
        inserted = write(db or get_supabase(), rows)
    except ValueError as exc:
        # Our own checks (repeated name or artist): the message is ours.
        logger.error("refusing to write: %s", exc)
        return 1
    except AppError as exc:
        logger.error("seed failed: %s", type(exc).__name__)
        return 1
    out.write(f"wrote {inserted} rows\n")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
