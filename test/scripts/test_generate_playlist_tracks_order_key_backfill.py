# test/scripts/test_generate_playlist_tracks_order_key_backfill.py
#
# Tests for the playlist_tracks.order_key backfill generator.
#
# Tested:
# - order_keys(N) sorts bytewise in position order (COLLATE "C" order)
# - order_keys(N) has no duplicates
# - order_keys(m) is a prefix of order_keys(N) for m < N (key i does not
#   depend on N)
# - Every key is ASCII alphanumeric, so it needs no SQL escaping
# - order_keys() raises ValueError if a generated key is not ASCII
#   alphanumeric
# - db/backfills/playlist_tracks_order_key.sql matches what the generator
#   produces for N, byte for byte
# - N is 10000
# - The lookup covers position 1..N exactly once, in order, with the keys
#   order_keys(N) produces
# - The generated file has no CR
# - The generated file writes only order_key, never position
# - The top-level statements are BEGIN;, the DISABLE, the UPDATE, the
#   ENABLE, the DO block and COMMIT;, in that exact order
# - trg_bump_playlist_on_track_change is disabled once and re-enabled
#   once, with the ENABLE between the end of the UPDATE and COMMIT;
# - The DO $$ ... $$; assertion block sits between the ENABLE and
#   COMMIT;, with three RAISE EXCEPTION
# - trg_playlist_tracks_reorder never appears in a non-comment line
# - main() writes render_sql(N) verbatim to the given path
#
# What is covered:
# - No database, no HTTP: the script has no boundary to mock. Every test
#   runs against the pure Python functions and the versioned .sql file.
#
# Run with: pytest test/scripts/test_generate_playlist_tracks_order_key_backfill.py -v
#
# SEE: scripts/generate_playlist_tracks_order_key_backfill.py,
#      db/backfills/playlist_tracks_order_key.sql

import re

import pytest

import scripts.generate_playlist_tracks_order_key_backfill as gen


def test_keys_sort_bytewise_in_position_order():
    keys = gen.order_keys(gen.N)
    for i in range(len(keys) - 1):
        assert keys[i].encode("ascii") < keys[i + 1].encode("ascii")


def test_keys_are_unique():
    keys = gen.order_keys(gen.N)
    assert len(set(keys)) == gen.N


def test_keys_are_prefix_stable():
    full = gen.order_keys(gen.N)
    for m in (1, 62, 63, 1000, gen.N):
        assert gen.order_keys(m) == full[:m]


def test_keys_are_ascii_alphanumeric():
    for key in gen.order_keys(gen.N):
        assert key.isascii()
        assert key.isalnum()


def test_order_keys_rejects_non_alphanumeric_key(monkeypatch):
    monkeypatch.setattr(gen, "generate_n_keys_between", lambda a, b, n: ["a0", "a'1"])
    with pytest.raises(ValueError):
        gen.order_keys(2)


def test_versioned_backfill_matches_generator():
    assert gen.OUTPUT.read_bytes() == gen.render_sql(gen.N).encode("utf-8")
    assert gen.OUTPUT.name == "playlist_tracks_order_key.sql"
    assert gen.OUTPUT.parent.name == "backfills"


def test_n_is_ten_thousand():
    assert gen.N == 10000


def test_backfill_lookup_covers_every_position_once():
    text = gen.OUTPUT.read_text(encoding="utf-8")
    keys = gen.order_keys(gen.N)
    rows = re.findall(r"\((\d+), '([^']*)'\)", text)
    assert len(rows) == gen.N
    for i, (position_str, key) in enumerate(rows):
        assert int(position_str) == i + 1
        assert key == keys[i]


def test_backfill_is_lf_only():
    assert b"\r" not in gen.OUTPUT.read_bytes()


def test_backfill_writes_only_order_key():
    text = gen.OUTPUT.read_text(encoding="utf-8")
    assert text.count("UPDATE public.playlist_tracks") == 1
    assert "SET order_key = k.order_key" in text
    assert "SET position" not in text
    for match in re.finditer(r"position\s*=", text):
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.end())
        line = text[line_start:line_end]
        assert line.strip() == "WHERE pt.position = k.position"


def _top_level_lines(text: str) -> list[str]:
    return [
        line
        for line in text.split("\n")
        if line and not line.startswith(" ") and not line.startswith("--")
    ]


def test_backfill_top_level_statement_order():
    text = gen.render_sql(gen.N)
    assert _top_level_lines(text) == [
        "BEGIN;",
        "ALTER TABLE public.playlist_tracks DISABLE TRIGGER trg_bump_playlist_on_track_change;",
        "UPDATE public.playlist_tracks AS pt",
        "ALTER TABLE public.playlist_tracks ENABLE TRIGGER trg_bump_playlist_on_track_change;",
        "DO $$",
        "$$;",
        "COMMIT;",
    ]


def test_backfill_reenables_bump_trigger_before_commit():
    text = gen.render_sql(gen.N)
    assert text.count("DISABLE TRIGGER trg_bump_playlist_on_track_change") == 1
    assert text.count("ENABLE TRIGGER trg_bump_playlist_on_track_change") == 1

    lines = text.split("\n")
    update_end = next(
        i
        for i, line in enumerate(lines)
        if line.strip() == "AND pt.order_key IS DISTINCT FROM k.order_key;"
    )
    enable_line = next(
        i
        for i, line in enumerate(lines)
        if "ENABLE TRIGGER trg_bump_playlist_on_track_change" in line
    )
    commit_line = next(i for i, line in enumerate(lines) if line == "COMMIT;")
    last_non_empty = max(i for i, line in enumerate(lines) if line.strip())

    assert update_end < enable_line < commit_line
    assert commit_line == last_non_empty


def test_backfill_asserts_before_commit():
    text = gen.render_sql(gen.N)
    lines = text.split("\n")
    enable_line = next(
        i
        for i, line in enumerate(lines)
        if "ENABLE TRIGGER trg_bump_playlist_on_track_change" in line
    )
    do_line = next(i for i, line in enumerate(lines) if line == "DO $$")
    do_close_line = next(i for i, line in enumerate(lines) if line == "$$;")
    commit_line = next(i for i, line in enumerate(lines) if line == "COMMIT;")

    assert enable_line < do_line < do_close_line < commit_line
    block = "\n".join(lines[do_line : do_close_line + 1])
    assert block.count("RAISE EXCEPTION") == 3


def test_backfill_never_touches_reorder_trigger():
    text = gen.render_sql(gen.N)
    for line in text.split("\n"):
        if line.startswith("--"):
            continue
        assert "trg_playlist_tracks_reorder" not in line


def test_main_writes_render_to_given_path(tmp_path):
    out = tmp_path / "sub" / "out.sql"
    gen.main(out)
    assert out.read_bytes() == gen.render_sql(gen.N).encode("utf-8")
