# test/scripts/test_seed_onboarding_artists.py
#
# Tests for the seed script of the curated onboarding artists.
#
# Tested:
# - The versioned file lists the thirty names of the issue, in order
# - read_names skips blank lines and rejects a repeated name
# - The dry run (the default) prints each name with the artist it resolves
#   to, writes nothing and creates no database client; a name without a
#   result is reported
# - --write replaces the table with the matches in file order, with
#   contiguous positions from 1; a name without result is skipped and the
#   positions stay contiguous
# - --write refuses (exit 1, no write) when two names resolve to the same
#   artist and when nothing matched
# - A count that is not the rows sent, a provider failure and a database
#   failure exit 1 with nothing written (or no success line)
#
# What is covered:
# - scripts/seed_onboarding_artists.py with a fake provider and a fake
#   database injected in main(): no network, no Supabase. The RPC against the
#   real schema is in test/integration/test_onboarding_integration.py
#
# Run with: pytest test/scripts/test_seed_onboarding_artists.py -v
#
# SEE: scripts/seed_onboarding_artists.py,
# db/migrations/049_onboarding_artists.sql

import io
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests
from postgrest.exceptions import APIError

from scripts import seed_onboarding_artists as seed

_HOST = "https://lh3.googleusercontent.com"

_ISSUE_NAMES = [
    "Taylor Swift",
    "Drake",
    "The Weeknd",
    "Billie Eilish",
    "Justin Bieber",
    "Sabrina Carpenter",
    "Dua Lipa",
    "Ariana Grande",
    "Ed Sheeran",
    "Olivia Rodrigo",
    "Bruno Mars",
    "Lady Gaga",
    "Kendrick Lamar",
    "SZA",
    "Travis Scott",
    "Post Malone",
    "Harry Styles",
    "Coldplay",
    "Bad Bunny",
    "Karol G",
    "Shakira",
    "Rosalia",
    "J Balvin",
    "Peso Pluma",
    "Bizarrap",
    "Duki",
    "Emilia",
    "Tini",
    "Maria Becerra",
    "CA7RIEL & Paco Amoroso",
]


def _row(name):
    slug = name.replace(" ", "")
    return {
        "browseId": f"UC{slug}",
        "artist": f"{name} (official)",
        "thumbnails": [{"url": f"{_HOST}/{slug}=w226-h226-l90-rj"}],
    }


def _provider(missing=(), same_as=None):
    # search(q, filter="artists", limit=1) answers one row per name.
    provider = MagicMock()

    def search(q, **_):
        if q in missing:
            return []
        return [_row((same_as or {}).get(q, q))]

    provider.search.side_effect = search
    return provider


def _db(count=None):
    db = MagicMock()

    def rpc(name, params):
        query = MagicMock()
        rows = params["p_rows"]
        query.execute.return_value = SimpleNamespace(
            data=len(rows) if count is None else count
        )
        return query

    db.rpc.side_effect = rpc
    return db


def _names_file(tmp_path, names):
    path = tmp_path / "names.txt"
    path.write_text("\n".join(names) + "\n", encoding="utf-8")
    return path


def _run(tmp_path, names, *args, provider=None, db=None):
    out = io.StringIO()
    code = seed.main(
        ["--names", str(_names_file(tmp_path, names)), *args],
        provider=provider or _provider(),
        db=db if db is not None else _db(),
        out=out,
    )
    return code, out.getvalue()


def _written(db):
    (call,) = db.rpc.call_args_list
    assert call.args[0] == "replace_onboarding_artists"
    return call.args[1]["p_rows"]


def test_the_versioned_file_lists_the_thirty_names_of_the_issue_in_order():
    assert seed.read_names(seed.NAMES_FILE) == _ISSUE_NAMES


def test_read_names_skips_blank_lines_and_rejects_repeated_names(tmp_path):
    path = tmp_path / "names.txt"
    path.write_text("One\n\n  Two  \n\n", encoding="utf-8")
    assert seed.read_names(path) == ["One", "Two"]

    path.write_text("One\nTwo\nOne\n", encoding="utf-8")
    with pytest.raises(ValueError):
        seed.read_names(path)


def test_dry_run_prints_each_name_with_the_artist_found_and_writes_nothing(tmp_path):
    db = _db()

    code, text = _run(tmp_path, ["One", "Two"], db=db)

    assert code == 0
    assert text == (
        "One -> UCOne (One (official))\nTwo -> UCTwo (Two (official))\n2 of 2 matched\n"
    )
    db.rpc.assert_not_called()


def test_dry_run_does_not_create_a_database_client(tmp_path, monkeypatch):
    def forbidden():
        raise AssertionError("no database client in a dry run")

    monkeypatch.setattr(seed, "get_supabase", forbidden)
    out = io.StringIO()

    code = seed.main(
        ["--names", str(_names_file(tmp_path, ["One"]))],
        provider=_provider(),
        out=out,
    )

    assert code == 0


def test_dry_run_reports_a_name_without_result(tmp_path):
    code, text = _run(
        tmp_path, ["One", "Nobody"], provider=_provider(missing={"Nobody"})
    )

    assert code == 0
    assert text == "One -> UCOne (One (official))\nNobody -> no match\n1 of 2 matched\n"


def test_write_replaces_the_table_with_the_matches_in_file_order(tmp_path):
    db = _db()

    code, text = _run(tmp_path, ["One", "Two", "Three"], "--write", db=db)

    assert code == 0
    assert text.endswith("wrote 3 rows\n")
    assert _written(db) == [
        {
            "id": f"UC{name}",
            "name": f"{name} (official)",
            "thumbnail_url": f"{_HOST}/{name}=w544-h544-p-l90-rj",
            "position": position,
        }
        for position, name in enumerate(["One", "Two", "Three"], start=1)
    ]


def test_write_skips_a_name_without_result_and_keeps_positions_contiguous(tmp_path):
    db = _db()

    code, _ = _run(
        tmp_path,
        ["One", "Nobody", "Three"],
        "--write",
        provider=_provider(missing={"Nobody"}),
        db=db,
    )

    assert code == 0
    rows = _written(db)
    assert [(r["id"], r["position"]) for r in rows] == [("UCOne", 1), ("UCThree", 2)]


def test_write_refuses_when_two_names_resolve_to_the_same_artist(tmp_path):
    db = _db()

    code, _ = _run(
        tmp_path,
        ["One", "Uno"],
        "--write",
        provider=_provider(same_as={"Uno": "One"}),
        db=db,
    )

    assert code == 1
    db.rpc.assert_not_called()


def test_write_with_no_matches_writes_nothing(tmp_path):
    db = _db()

    code, text = _run(
        tmp_path, ["Nobody"], "--write", provider=_provider(missing={"Nobody"}), db=db
    )

    assert code == 1
    assert "wrote" not in text
    db.rpc.assert_not_called()


def test_write_count_mismatch_exits_with_error(tmp_path):
    code, text = _run(tmp_path, ["One", "Two"], "--write", db=_db(count=1))

    assert code == 1
    assert "wrote" not in text


def test_write_database_failure_exits_with_error(tmp_path):
    db = MagicMock()
    db.rpc.return_value.execute.side_effect = APIError({"message": "boom"})

    code, text = _run(tmp_path, ["One"], "--write", db=db)

    assert code == 1
    assert "wrote" not in text


def test_a_provider_failure_exits_with_error_and_writes_nothing(tmp_path):
    provider = MagicMock()
    provider.search.side_effect = requests.exceptions.ReadTimeout("slow")
    db = _db()

    code, _ = _run(tmp_path, ["One"], "--write", provider=provider, db=db)

    assert code == 1
    db.rpc.assert_not_called()
