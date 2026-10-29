# test/core/test_search_provider.py
#
# Tests for how the external provider client is built.
#
# Tested:
# - _provider() builds the client with the bounded ProviderSession
#
# What is covered:
# - The wiring of core/search_provider.py to core/provider_session.py
#
# Run with: pytest test/core/test_search_provider.py -v
#
# SEE: core/search_provider.py, core/provider_session.py

from core import search_provider
from core.provider_session import ProviderSession


def test_provider_is_built_with_the_bounded_session(monkeypatch):
    built = {}

    def _recorder(**kwargs):
        built.update(kwargs)
        return object()

    monkeypatch.setattr(search_provider, "SearchProvider", _recorder)
    search_provider._provider.cache_clear()
    try:
        search_provider._provider()
    finally:
        search_provider._provider.cache_clear()

    assert isinstance(built["requests_session"], ProviderSession)
