# test/core/test_database.py
#
# Tests for the two Supabase clients: get_db (service-role, unchanged)
# and the per-user LRU cache behind get_user_client.
#
# Tested:
# - get_db still builds the service-role client with create_client(url,
#   service_role_key)
# - get_user_client builds the client with create_client(url, anon_key)
#   and authenticates it with client.postgrest.auth(token)
# - The same JWT reuses the cached client -- one creation only
# - A new JWT creates and caches a new client alongside the first
# - Evicting past the cache size closes the evicted client with
#   postgrest.aclose() and drops it from the cache
# - A cache hit refreshes recency: the least recently used entry is the
#   one evicted, not the one just hit
# - An evicted JWT gets a fresh client, not the one that was closed
# - Client creation and eviction both run with the cache lock held
# - Concurrent requests across more distinct tokens than the cache size
#   never leave more entries cached than the size, and every created
#   client that is not the one currently cached had its aclose() called
#   exactly once
# - A cached client's postgrest.auth() is never called a second time,
#   including by a different token
#
# What is covered:
# - The LRU cache's hit/miss/eviction/recency logic and its locking,
#   with create_client mocked so no real Supabase client is built
#
# Run with: pytest test/core/test_database.py -v
#
# SEE: core/database.py, core/auth.py

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core import database
from core.config import settings
from core.database import get_db, get_supabase, get_user_client


@pytest.fixture(autouse=True)
def user_client_cache(monkeypatch):
    # Clears the module-level cache before and after every test, and
    # replaces create_client with a factory that hands back a fresh
    # MagicMock on every call, recorded here so a test can inspect which
    # url/key it was built with.
    database._user_clients.clear()

    created = []

    def factory(url, key):
        client = MagicMock()
        created.append(SimpleNamespace(url=url, key=key, client=client))
        return client

    monkeypatch.setattr(database, "create_client", factory)

    yield created

    database._user_clients.clear()


def test_get_db_still_builds_the_service_role_client(user_client_cache):
    get_supabase.cache_clear()
    try:
        client = get_db()

        assert len(user_client_cache) == 1
        record = user_client_cache[0]
        assert record.url == settings.supabase_url
        assert record.key == settings.supabase_service_role_key
        assert client is record.client
    finally:
        get_supabase.cache_clear()


def test_user_client_uses_the_anon_key_and_the_user_jwt(user_client_cache):
    client = get_user_client("token-a")

    assert len(user_client_cache) == 1
    record = user_client_cache[0]
    assert record.url == settings.supabase_url
    assert record.key == settings.supabase_anon_key
    client.postgrest.auth.assert_called_once_with("token-a")


def test_same_jwt_reuses_the_cached_client(user_client_cache):
    first = get_user_client("token-a")
    second = get_user_client("token-a")

    assert first is second
    assert len(user_client_cache) == 1


def test_new_jwt_creates_and_caches_a_new_client(user_client_cache):
    first = get_user_client("token-a")
    second = get_user_client("token-b")

    assert first is not second
    assert len(user_client_cache) == 2
    assert list(database._user_clients) == ["token-a", "token-b"]


def test_eviction_past_the_size_closes_the_evicted_client(monkeypatch):
    monkeypatch.setattr(database, "_USER_CLIENT_CACHE_SIZE", 2)

    first = get_user_client("token-a")
    second = get_user_client("token-b")
    third = get_user_client("token-c")

    first.postgrest.aclose.assert_called_once()
    second.postgrest.aclose.assert_not_called()
    third.postgrest.aclose.assert_not_called()
    assert "token-a" not in database._user_clients
    assert list(database._user_clients) == ["token-b", "token-c"]


def test_a_hit_refreshes_recency(monkeypatch):
    monkeypatch.setattr(database, "_USER_CLIENT_CACHE_SIZE", 2)

    a = get_user_client("token-a")
    b = get_user_client("token-b")
    assert get_user_client("token-a") is a  # hit, moves token-a to the end
    get_user_client("token-c")  # evicts the least recently used: token-b

    a.postgrest.aclose.assert_not_called()
    b.postgrest.aclose.assert_called_once()
    assert "token-a" in database._user_clients
    assert "token-b" not in database._user_clients


def test_an_evicted_jwt_gets_a_fresh_client(monkeypatch):
    monkeypatch.setattr(database, "_USER_CLIENT_CACHE_SIZE", 1)

    first = get_user_client("token-a")
    get_user_client("token-b")  # evicts and closes token-a
    second = get_user_client("token-a")

    assert second is not first
    first.postgrest.aclose.assert_called_once()


def test_create_and_evict_run_under_the_lock(monkeypatch):
    monkeypatch.setattr(database, "_USER_CLIENT_CACHE_SIZE", 1)

    lock_held_on_create = []
    lock_held_on_close = []

    def factory(url, key):
        lock_held_on_create.append(database._user_clients_lock.locked())
        client = MagicMock()
        client.postgrest.aclose.side_effect = lambda: lock_held_on_close.append(
            database._user_clients_lock.locked()
        )
        return client

    monkeypatch.setattr(database, "create_client", factory)

    get_user_client("token-a")
    get_user_client(
        "token-b"
    )  # creates under the lock, evicts+closes token-a under it too

    assert lock_held_on_create == [True, True]
    assert lock_held_on_close == [True]


def test_concurrent_requests_never_exceed_the_size_or_leak_a_client(monkeypatch):
    monkeypatch.setattr(database, "_USER_CLIENT_CACHE_SIZE", 4)

    # ("create"|"evict", token or None, client) -- appended only from
    # inside client.postgrest.auth()/aclose(), which _create_user_client
    # and get_user_client's eviction branch only ever call while holding
    # _user_clients_lock, so this list's order is the real serialized
    # order of every creation and eviction, safe to build without a lock
    # of its own.
    events = []

    def factory(url, key):
        client = MagicMock()

        def record_auth(token, c=client):
            events.append(("create", token, c))

        def record_close(c=client):
            events.append(("evict", None, c))

        client.postgrest.auth.side_effect = record_auth
        client.postgrest.aclose.side_effect = record_close
        return client

    monkeypatch.setattr(database, "create_client", factory)

    tokens = [f"token-{i % 6}" for i in range(16)]
    barrier = threading.Barrier(len(tokens))

    def worker(token):
        barrier.wait()
        get_user_client(token)

    threads = [threading.Thread(target=worker, args=(token,)) for token in tokens]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(database._user_clients) <= 4

    client_to_token = {
        client: token for kind, token, client in events if kind == "create"
    }
    cached_clients = set(database._user_clients.values())
    for client in client_to_token:
        if client in cached_clients:
            client.postgrest.aclose.assert_not_called()
        else:
            client.postgrest.aclose.assert_called_once()

    # No token was ever created a second time while its previous client
    # was still live (not yet evicted) -- the double-checked-locking bug
    # this test exists to catch: creation moved outside the lock would
    # let two threads racing on the same new token both miss the cache
    # and both create a client for it.
    live_tokens = set()
    for kind, token, client in events:
        if kind == "create":
            assert token not in live_tokens, (
                f"{token} was created twice with no eviction in between"
            )
            live_tokens.add(token)
        else:
            live_tokens.discard(client_to_token[client])


def test_a_cached_client_is_never_reauthenticated():
    a = get_user_client("token-a")
    b = get_user_client("token-b")
    get_user_client("token-a")
    get_user_client("token-b")
    get_user_client("token-a")
    c = get_user_client("token-c")

    a.postgrest.auth.assert_called_once_with("token-a")
    b.postgrest.auth.assert_called_once_with("token-b")
    c.postgrest.auth.assert_called_once_with("token-c")
