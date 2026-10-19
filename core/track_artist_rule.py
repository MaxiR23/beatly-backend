# INFO: Decides what to do when a model rejects the track artists rule.

import logging
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from models.artist_refs import TRACK_ARTISTS_REASONS

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def artist_rule_reason(exc: ValidationError) -> str | None:
    # A readable reason only when every error is one of the rule's two
    # types. Any other error, alone or next to the rule's, or none at all,
    # is not "just the rule" and gives None.
    errors = exc.errors(include_input=False, include_url=False)
    if not errors or any(
        error["type"] not in TRACK_ARTISTS_REASONS for error in errors
    ):
        return None
    return TRACK_ARTISTS_REASONS[errors[0]["type"]]


def build_track(kind: str, model: type[T], fields: dict[str, Any]) -> T | None:
    # Not the swallow-and-return-empty antipattern of CLAUDE.md: only
    # ValidationError is caught, everything that is not the rule is
    # re-raised for translate_upstream_errors() to turn into a 502, and
    # what is dropped is logged and returned as None for that one item,
    # never as an empty list. It is the repo owner's decision for songs the
    # provider lists without a usable artist, and the same shape as
    # services/search_service.py::_build (#182). The log carries the kind of
    # list and the reason, never the fields, the error text (it holds the
    # input) or a traceback.
    try:
        return model(**fields)
    except ValidationError as exc:
        reason = artist_rule_reason(exc)
        if reason is None:
            raise
        logger.warning("%s skipped, artists: %s", kind, reason)
        return None
