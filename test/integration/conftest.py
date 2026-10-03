# test/integration/conftest.py
#
# Harness for the integration tests: the real app, with no dependency
# overrides, against the local Supabase stack built from db/migrations.
#
# Tested:
# - Aborts the whole session when SUPABASE_URL does not point at the local
#   stack, so these tests can never create users or rows in a remote project
# - Gives each test a fresh user (GoTrue admin, service-role) and a JWT
#   signed with the local secret, so RLS runs for real in get_user_db
# - Seeds tracks and genre playlists with unique ids through the
#   service-role client
#
# What is covered:
# - Fixtures only, no tests of its own
#
# Run with: pytest -m integration
# (needs the local stack, see docs/testing.md)
#
# SEE: db/local/apply_migrations.sh, core/database.py, core/auth.py

import time
from dataclasses import dataclass
from urllib.parse import urlparse
from uuid import uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

from app import app
from core.config import settings
from core.database import get_supabase

_LOCAL_HOSTS = {"127.0.0.1", "localhost"}

# A thumbnail the square rewrite recognizes: the tests assert the 544 form.
_THUMBNAIL_60 = "https://lh3.googleusercontent.com/abc=w60-h60-l90-rj"


@dataclass(frozen=True)
class User:
    user_id: str
    token: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


@pytest.fixture(scope="session", autouse=True)
def _only_against_the_local_stack():
    # The SUPABASE_* variables come from the process environment, which
    # pydantic-settings prefers over .env. Without them the app would point
    # at whatever .env says, possibly production.
    host = urlparse(settings.supabase_url).hostname
    if host not in _LOCAL_HOSTS:
        pytest.exit(
            "integration tests only run against the local Supabase stack: "
            "SUPABASE_URL must point at 127.0.0.1 or localhost "
            "(see docs/testing.md)",
            returncode=2,
        )


@pytest.fixture(scope="session")
def admin():
    # Service-role client of the local stack: seeds data and reads back the
    # rows the tests verify.
    return get_supabase()


@pytest.fixture
def client():
    # These tests exercise the real dependencies, so nothing may be
    # overridden.
    assert app.dependency_overrides == {}
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def make_user(admin):
    def _make_user(role: str | None = None) -> User:
        created = admin.auth.admin.create_user(
            {
                "email": f"it-{uuid4().hex}@example.com",
                "password": uuid4().hex,
                "email_confirm": True,
            }
        )
        user_id = created.user.id

        if role is not None:
            # The enforce_role_change_permission trigger allows it with a
            # null auth.uid(), which is what the service-role has.
            admin.table("profiles").update({"role": role}).eq("id", user_id).execute()

        token = jwt.encode(
            {
                "sub": user_id,
                "aud": "authenticated",
                "role": "authenticated",
                "exp": int(time.time()) + 3600,
            },
            settings.supabase_jwt_secret,
            algorithm="HS256",
        )
        return User(user_id=user_id, token=token)

    return _make_user


@pytest.fixture
def track_payload():
    def _track_payload(**overrides) -> dict:
        return {
            "track_id": f"it-{uuid4().hex}",
            "title": "Integration Track",
            "artists": [{"id": "artist-1", "name": "Some Artist"}],
            "album": "Integration Album",
            "album_id": "album-1",
            "thumbnail_url": _THUMBNAIL_60,
            "duration_seconds": 200,
            **overrides,
        }

    return _track_payload


@pytest.fixture
def seed_tracks(admin, track_payload):
    def _seed_tracks(count: int = 1) -> list[dict]:
        tracks = [track_payload() for _ in range(count)]
        admin.table("tracks").upsert(tracks, on_conflict="track_id").execute()
        return tracks

    return _seed_tracks


@pytest.fixture
def seed_genre_playlist(admin, seed_tracks):
    def _seed_genre_playlist(track_count: int = 2) -> dict:
        suffix = uuid4().hex[:12]
        genre = (
            admin.table("genres")
            .insert({"name": f"Genre {suffix}", "slug": f"it-{suffix}"})
            .execute()
            .data[0]
        )
        playlist = (
            admin.table("genre_playlists")
            .insert(
                {
                    "genre_id": genre["id"],
                    "title": f"Genre Playlist {suffix}",
                    "category": f"Category {suffix}",
                }
            )
            .execute()
            .data[0]
        )
        tracks = seed_tracks(track_count)
        admin.table("genre_playlist_tracks").insert(
            [
                {
                    "playlist_id": playlist["id"],
                    "track_id": track["track_id"],
                    "position": index,
                }
                for index, track in enumerate(tracks, start=1)
            ]
        ).execute()
        return {"genre": genre, "playlist": playlist, "tracks": tracks}

    return _seed_genre_playlist
