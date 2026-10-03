# test/integration/test_schema_integration.py
#
# Tests of the integration harness itself: that a query written against a
# column the schema does not have fails against the local database, and
# that the app turns it into the contract's upstream error.
#
# Tested:
# - A select on a missing column raises postgrest APIError 42703
# - The service layer turns that into UpstreamError
# - GET /likes with the pre-#176 column list answers 502 upstream_error:
#   reverting the fix to _COLUMNS makes an integration test fail
#
# What is covered:
# - The harness sees schema drift through the real app
#
# Run with: pytest -m integration test/integration/test_schema_integration.py -v
#
# SEE: services/likes_service.py, core/upstream.py

import pytest
from postgrest.exceptions import APIError

from core.database import get_user_client
from core.exceptions import UpstreamError
from core.upstream import translate_upstream_errors
from services import likes_service

pytestmark = pytest.mark.integration

_OLD_COLUMNS = (
    "track_id, title, artists, album, album_id, thumbnail_url, "
    "duration_seconds, created_at, updated_at, deleted_at"
)


def test_a_query_for_a_missing_column_fails_against_the_local_database(make_user):
    user = make_user()

    with pytest.raises(APIError) as raised:
        get_user_client(user.token).table("user_likes").select(
            "track_id, title"
        ).execute()

    assert raised.value.code == "42703"


def test_the_service_layer_turns_a_missing_column_into_upstream_error(make_user):
    user = make_user()

    with pytest.raises(UpstreamError), translate_upstream_errors():
        get_user_client(user.token).table("user_likes").select(
            "track_id, title"
        ).execute()


def test_get_likes_with_the_old_user_likes_columns_is_upstream_error(
    client, make_user, monkeypatch
):
    user = make_user()
    monkeypatch.setattr(likes_service, "_COLUMNS", _OLD_COLUMNS)

    response = client.get("/likes", headers=user.headers)

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
