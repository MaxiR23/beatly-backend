# INFO: Two Supabase clients: get_db, service-role, for catalog/public
# domains; get_user_client, anon key + the caller's JWT, cached per token,
# for the six user-data domains.

import threading
from collections import OrderedDict
from functools import lru_cache

from supabase import Client, create_client

from core.config import settings


@lru_cache
def get_supabase() -> Client:
    return create_client(
        settings.supabase_url,
        settings.supabase_service_role_key,
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
    client = create_client(settings.supabase_url, settings.supabase_anon_key)
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
