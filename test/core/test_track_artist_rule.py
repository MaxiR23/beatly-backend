# test/core/test_track_artist_rule.py
#
# Tests for the helper that decides what to do when a model rejects the
# track artists rule.
#
# Tested:
# - artist_rule_reason returns the readable reason for each of the two
#   rule errors
# - artist_rule_reason returns None for a mixed error (the rule plus
#   another field) and for an error that is not the rule
# - build_track returns the model when it is valid
# - build_track returns None and logs the kind and the reason, never the
#   name, the id or a traceback
# - build_track re-raises a validation error that is not only the rule
#
# What is covered:
# - Both reasons, mixed and plain errors, the log content and the re-raise
#
# Run with: pytest test/core/test_track_artist_rule.py -v
#
# SEE: core/track_artist_rule.py, models/artist_refs.py

import logging

import pytest
from pydantic import BaseModel, ValidationError

from core.track_artist_rule import artist_rule_reason, build_track
from models.artist_refs import TrackArtists


class _Song(BaseModel):
    title: str
    artists: TrackArtists


def _validation_error(**fields) -> ValidationError:
    with pytest.raises(ValidationError) as exc:
        _Song(**fields)
    return exc.value


def test_artist_rule_reason_for_an_empty_list():
    assert artist_rule_reason(_validation_error(title="T", artists=[])) == "no artists"


def test_artist_rule_reason_for_a_primary_without_id():
    exc = _validation_error(title="T", artists=[{"id": None, "name": "A"}])

    assert artist_rule_reason(exc) == "primary artist without id"


def test_artist_rule_reason_is_none_for_a_mixed_error():
    assert artist_rule_reason(_validation_error(artists=[])) is None


def test_artist_rule_reason_is_none_for_an_error_that_is_not_the_rule():
    exc = _validation_error(title=5, artists=[{"id": "a", "name": "A"}])

    assert artist_rule_reason(exc) is None


def test_build_track_returns_the_model_when_valid():
    song = build_track(
        "song", _Song, {"title": "T", "artists": [{"id": "a", "name": "A"}]}
    )

    assert song is not None
    assert song.title == "T"


def test_build_track_returns_none_and_logs_kind_and_reason(caplog):
    fields = {"title": "Secret Title", "artists": [{"id": None, "name": "Secret Name"}]}

    with caplog.at_level(logging.WARNING, logger="core.track_artist_rule"):
        result = build_track("upnext track", _Song, fields)

    assert result is None
    assert [r.getMessage() for r in caplog.records] == [
        "upnext track skipped, artists: primary artist without id"
    ]
    assert caplog.records[0].exc_info is None
    assert "Secret" not in caplog.text


def test_build_track_reraises_a_non_rule_validation_error():
    with pytest.raises(ValidationError):
        build_track("song", _Song, {"title": 5, "artists": []})
