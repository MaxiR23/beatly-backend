# test/core/test_auth.py
#
# Tests for the auth dependencies.
#
# Tested:
# - A valid JWT resolves to its user id via get_current_user_id
# - A missing Authorization header returns 401 unauthorized
# - A malformed token returns 401 unauthorized
# - An expired token returns 401 unauthorized
# - A token with the wrong audience returns 401 unauthorized
# - get_current_profile returns the profile matching the token's user id
# - get_current_profile returns 404 profile_not_found when no profile
#   row matches the user id
# - get_current_profile returns 502 via UpstreamError when the profile
#   lookup fails
# - get_current_profile returns 504 via UpstreamTimeout when the profile
#   lookup times out
# - require_role returns 403 forbidden when the profile's role ranks
#   below the required role
# - require_role passes when the profile's role ranks at the required role
# - require_role passes when the profile's role ranks above the required
#   role (admin satisfies a tester-gated route), proving the hierarchy
#
# What is covered:
# - Happy path, missing/invalid/expired token, wrong audience, expected
#   missing-profile state, upstream failure, upstream timeout, role
#   hierarchy (below/at/above)
#
# Run with: pytest test/core/test_auth.py -v
#
# SEE: core/auth.py, services/profile_service.py, models/profiles.py

import time
from unittest.mock import MagicMock

import httpx
import jwt
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

import core.auth as auth_module
from app import app
from core.auth import get_current_profile, get_current_user_id, require_role
from core.database import get_db
from models.profiles import Profile, Role

client = TestClient(app, raise_server_exceptions=False)

_TEST_SECRET = "test-secret-at-least-32-bytes-long"
_USER_ID = "11111111-1111-1111-1111-111111111111"


@app.get("/_test/auth/user-id")
def _user_id_route(user_id: str = Depends(get_current_user_id)):
    return {"user_id": user_id}


@app.get("/_test/auth/profile")
def _profile_route(profile: Profile = Depends(get_current_profile)):  # noqa: B008
    return {"id": profile.id, "role": profile.role.value}


@app.get("/_test/auth/tester-only")
def _tester_only_route(
    profile: Profile = Depends(require_role(Role.TESTER)),  # noqa: B008
):
    return {"id": profile.id, "role": profile.role.value}


@pytest.fixture(autouse=True)
def _patch_secret(monkeypatch):
    monkeypatch.setattr(auth_module.settings, "supabase_jwt_secret", _TEST_SECRET)
    yield


@pytest.fixture(autouse=True)
def _clear_db_override():
    yield
    app.dependency_overrides.pop(get_db, None)


def _make_token(sub=_USER_ID, exp_delta=3600, aud="authenticated", secret=_TEST_SECRET):
    payload = {"sub": sub, "aud": aud, "exp": int(time.time()) + exp_delta}
    return jwt.encode(payload, secret, algorithm="HS256")


def _auth_header(token):
    return {"Authorization": f"Bearer {token}"}


def _fake_profile_db(data=None, error=None):
    db = MagicMock()
    query = db.table.return_value.select.return_value.eq.return_value
    if error is not None:
        query.execute.side_effect = error
    else:
        query.execute.return_value = MagicMock(data=data)
    return db


def _use_profile(role, user_id=_USER_ID):
    app.dependency_overrides[get_db] = lambda: _fake_profile_db(
        data=[{"id": user_id, "role": role.value}]
    )


def test_valid_token_resolves_user_id():
    response = client.get("/_test/auth/user-id", headers=_auth_header(_make_token()))

    assert response.status_code == 200
    assert response.json() == {"user_id": _USER_ID}


def test_missing_token_returns_unauthorized():
    response = client.get("/_test/auth/user-id")

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


def test_malformed_token_returns_unauthorized():
    response = client.get(
        "/_test/auth/user-id", headers=_auth_header("not-a-valid-jwt")
    )

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


def test_expired_token_returns_unauthorized():
    token = _make_token(exp_delta=-60)

    response = client.get("/_test/auth/user-id", headers=_auth_header(token))

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


def test_wrong_audience_returns_unauthorized():
    token = _make_token(aud="service_role")

    response = client.get("/_test/auth/user-id", headers=_auth_header(token))

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


def test_current_profile_returns_matching_profile():
    _use_profile(Role.USER)

    response = client.get("/_test/auth/profile", headers=_auth_header(_make_token()))

    assert response.status_code == 200
    assert response.json() == {"id": _USER_ID, "role": "user"}


def test_current_profile_missing_returns_profile_not_found():
    app.dependency_overrides[get_db] = lambda: _fake_profile_db(data=[])

    response = client.get("/_test/auth/profile", headers=_auth_header(_make_token()))

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "profile_not_found"}


def test_current_profile_lookup_failure_returns_upstream_error():
    app.dependency_overrides[get_db] = lambda: _fake_profile_db(
        error=APIError({"message": "connection refused"})
    )

    response = client.get("/_test/auth/profile", headers=_auth_header(_make_token()))

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_current_profile_lookup_timeout_returns_upstream_timeout():
    app.dependency_overrides[get_db] = lambda: _fake_profile_db(
        error=httpx.ReadTimeout("timed out")
    )

    response = client.get("/_test/auth/profile", headers=_auth_header(_make_token()))

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_require_role_below_requirement_returns_forbidden():
    _use_profile(Role.USER)

    response = client.get(
        "/_test/auth/tester-only", headers=_auth_header(_make_token())
    )

    assert response.status_code == 403
    assert response.json() == {"ok": False, "reason": "forbidden"}


def test_require_role_at_requirement_passes():
    _use_profile(Role.TESTER)

    response = client.get(
        "/_test/auth/tester-only", headers=_auth_header(_make_token())
    )

    assert response.status_code == 200
    assert response.json() == {"id": _USER_ID, "role": "tester"}


def test_require_role_above_requirement_passes():
    _use_profile(Role.ADMIN)

    response = client.get(
        "/_test/auth/tester-only", headers=_auth_header(_make_token())
    )

    assert response.status_code == 200
    assert response.json() == {"id": _USER_ID, "role": "admin"}
