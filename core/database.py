# INFO: Two Supabase clients: get_db, service-role, for catalog/public
# domains; get_user_client, anon key + the caller's JWT, cached per token,
# for the six user-data domains.

import threading
from collections import OrderedDict
from functools import lru_cache

from supabase import Client, ClientOptions, create_client

from core.config import settings
from core.supabase_transport import build_supabase_http_client


@lru_cache
def get_supabase() -> Client:
    # Stays on HTTP/2 (ADR 017). The injected client carries the timeouts
    # and the one GET retry (ADR 018).
    return create_client(
        settings.supabase_url,
        settings.supabase_service_role_key,
        ClientOptions(httpx_client=build_supabase_http_client(http2=True)),
    )


def get_db() -> Client:
    return get_supabase()


# Each cached client owns its own httpx.Client with its own connection
# pool, which in turn owns file descriptors -- an unbounded cache is an
# unbounded number of open sockets. 256 is deliberately held well past
# the size of FastAPI's default AnyIO threadpool (40 threads): only more
# than 256 distinct new JWTs arriving while a single request is still in
# flight could make the cache evict a client another thread is still
# using (see the ADR for that residual risk).
_USER_CLIENT_CACHE_SIZE = 256

_user_clients: OrderedDict[str, Client] = OrderedDict()
_user_clients_lock = threading.Lock()


def _create_user_client(token: str) -> Client:
    # The client is shared by the threadpool's parallel requests. With
    # HTTP/2 they multiplex one connection, and a read error on it fails
    # every stream at once (several 502s together); HTTP/1.1 confines the
    # failure to one request. Timeouts (Settings, ADR 018) and redirects are
    # set on the injected client because postgrest does not apply its own to
    # it. The eviction's postgrest.aclose() closes this client (it is the
    # session) and, through it, the transport.
    http_client = build_supabase_http_client(http2=False)
    client = create_client(
        settings.supabase_url,
        settings.supabase_anon_key,
        ClientOptions(httpx_client=http_client),
    )
    client.postgrest.auth(token)
    return client


def get_user_client(token: str) -> Client:
    with _user_clients_lock:
        cached = _user_clients.get(token)
        if cached is not None:
            _user_clients.move_to_end(token)
            return cached

        client = _create_user_client(token)
        _user_clients[token] = client

        if len(_user_clients) > _USER_CLIENT_CACHE_SIZE:
            _, evicted = _user_clients.popitem(last=False)
            evicted.postgrest.aclose()

        return client
