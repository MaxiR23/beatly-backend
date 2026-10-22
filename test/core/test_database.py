# test/core/test_database.py
#
# Tests for the two Supabase clients: get_db (service-role)
# and the per-user LRU cache behind get_user_client.
#
# Tested:
# - get_db builds the service-role client with create_client(url,
#   service_role_key, ClientOptions with an injected httpx_client)
# - get_user_client builds the client with create_client(url, anon_key,
#   ClientOptions with an injected httpx_client) and authenticates it
#   with client.postgrest.auth(token)
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
# - The real per-user client talks HTTP/1.1 with the Settings timeouts and
#   follows redirects; the real get_db client does the same over HTTP/2
# - Neither real client emits postgrest's timeout DeprecationWarning
# - Evicting a real per-user client closes its injected httpx.Client and
#   the transport inside it
#
# What is covered:
# - The LRU cache's hit/miss/eviction/recency logic and its locking,
#   with create_client mocked so no real Supabase client is built (except
#   in the two real-client tests)
# - The real per-user client (create_client not mocked, no network):
#   HTTP/1.1 pool, the Settings timeouts, redirects followed, and the
#   injected httpx.Client and its transport closed on eviction
#
# Run with: pytest test/core/test_database.py -v
#
# SEE: core/database.py, core/auth.py

import threading
import warnings
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from supabase import ClientOptions
from supabase import create_client as real_create_client

from core import database
from core.config import settings
from core.database import get_db, get_supabase, get_user_client
from core.supabase_transport import SupabaseTransport


@pytest.fixture(autouse=True)
def user_client_cache(monkeypatch):
    # Clears the module-level cache before and after every test, and
    # replaces create_client with a factory that hands back a fresh
    # MagicMock on every call, recorded here so a test can inspect which
    # url/key it was built with.
    database._user_clients.clear()

    created = []

    def factory(url, key, *options):
        client = MagicMock()
        created.append(
            SimpleNamespace(url=url, key=key, options=options, client=client)
        )
        return client

    monkeypatch.setattr(database, "create_client", factory)

    yield created

    database._user_clients.clear()


def test_get_db_builds_the_service_role_client_with_an_injected_http_client(
    user_client_cache,
):
    get_supabase.cache_clear()
    try:
        client = get_db()

        assert len(user_client_cache) == 1
        record = user_client_cache[0]
        assert record.url == settings.supabase_url
        assert record.key == settings.supabase_service_role_key
        assert len(record.options) == 1
        assert isinstance(record.options[0], ClientOptions)
        assert isinstance(record.options[0].httpx_client, httpx.Client)
        assert client is record.client
    finally:
        get_supabase.cache_clear()


def test_user_client_uses_the_anon_key_and_the_user_jwt(user_client_cache):
    client = get_user_client("token-a")

    assert len(user_client_cache) == 1
    record = user_client_cache[0]
    assert record.url == settings.supabase_url
    assert record.key == settings.supabase_anon_key
    assert len(record.options) == 1
    assert isinstance(record.options[0], ClientOptions)
    assert isinstance(record.options[0].httpx_client, httpx.Client)
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

    def factory(url, key, options=None):
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

    def factory(url, key, options=None):
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


def _settings_timeout():
    return httpx.Timeout(
        connect=settings.supabase_connect_timeout,
        read=settings.supabase_read_timeout,
        write=settings.supabase_write_timeout,
        pool=settings.supabase_pool_timeout,
    )


def test_user_client_talks_http_1_1_with_the_settings_timeouts_and_redirects(
    monkeypatch,
):
    monkeypatch.setattr(database, "create_client", real_create_client)
    monkeypatch.setattr(database.settings, "supabase_url", "http://localhost:54321")
    monkeypatch.setattr(database.settings, "supabase_anon_key", "anon-key")

    client = database._create_user_client("token-a")
    session = client.postgrest.session
    try:
        assert isinstance(session, httpx.Client)
        assert session.timeout == _settings_timeout()
        assert session.follow_redirects is True
        # _http2 is a private attribute of httpcore's ConnectionPool: there
        # is no public API to read the protocol before connecting. httpx and
        # httpcore are pinned in requirements.txt, so a version change that
        # breaks this arrives together with this test.
        assert isinstance(session._transport, SupabaseTransport)
        assert session._transport._inner._pool._http2 is False
        assert client.postgrest.headers["Authorization"] == "Bearer token-a"
    finally:
        session.close()


def test_get_db_talks_http_2_with_the_settings_timeouts_and_redirects(monkeypatch):
    monkeypatch.setattr(database, "create_client", real_create_client)
    monkeypatch.setattr(database.settings, "supabase_url", "http://localhost:54321")
    monkeypatch.setattr(database.settings, "supabase_service_role_key", "service-key")
    get_supabase.cache_clear()
    try:
        session = get_db().postgrest.session
        try:
            assert isinstance(session, httpx.Client)
            assert session.timeout == _settings_timeout()
            assert session.follow_redirects is True
            assert isinstance(session._transport, SupabaseTransport)
            assert session._transport._inner._pool._http2 is True
        finally:
            session.close()
    finally:
        get_supabase.cache_clear()


def test_injected_clients_emit_no_postgrest_deprecation_warning(monkeypatch):
    monkeypatch.setattr(database, "create_client", real_create_client)
    monkeypatch.setattr(database.settings, "supabase_url", "http://localhost:54321")
    monkeypatch.setattr(database.settings, "supabase_anon_key", "anon-key")
    monkeypatch.setattr(database.settings, "supabase_service_role_key", "service-key")
    get_supabase.cache_clear()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            service = get_db()
            user = database._create_user_client("token-a")
            sessions = [service.postgrest.session, user.postgrest.session]
        for session in sessions:
            session.close()
    finally:
        get_supabase.cache_clear()


def test_evicting_a_real_user_client_closes_its_httpx_client(monkeypatch):
    monkeypatch.setattr(database, "create_client", real_create_client)
    monkeypatch.setattr(database.settings, "supabase_url", "http://localhost:54321")
    monkeypatch.setattr(database.settings, "supabase_anon_key", "anon-key")
    monkeypatch.setattr(database, "_USER_CLIENT_CACHE_SIZE", 1)

    first = get_user_client("token-a")
    session = first.postgrest.session
    assert not session.is_closed
    inner_closes = []
    inner = session._transport._inner
    real_close = inner.close
    monkeypatch.setattr(
        inner, "close", lambda: (inner_closes.append(True), real_close())
    )

    get_user_client("token-b")

    assert session.is_closed
    assert inner_closes == [True]
    assert first.auth._http_client.is_closed
