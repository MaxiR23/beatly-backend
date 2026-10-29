# test/fake_provider_http.py
#
# Fakes shared by the tests of the external provider session: an adapter that
# stands in for the network and a clock that moves by hand. Nothing here
# touches the real network.
#
# Tested:
# - Helpers only, no tests of its own
#
# What is covered:
# - Fixtures and helpers only
#
# Run with: pytest test/core/test_provider_session.py test/routes/test_search.py test/test_provider_deadline_wiring.py -v
#
# SEE: core/provider_session.py

import io

import urllib3
from requests.adapters import BaseAdapter, HTTPAdapter

BASE_URL = "https://provider.test"


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _build(request, raw):
    return HTTPAdapter().build_response(request, raw)


def body_response(request, body: bytes = b'{"ok": true}'):
    raw = urllib3.HTTPResponse(
        body=io.BytesIO(body),
        status=200,
        headers={"Content-Type": "application/json"},
        preload_content=False,
    )
    return _build(request, raw)


class FakeAdapter(BaseAdapter):
    # handler(request) returns a requests.Response, or raises. The kwargs of
    # every send are recorded, so a test can read the timeout.
    def __init__(self, handler=None) -> None:
        super().__init__()
        self.handler = handler or body_response
        self.calls: list[dict] = []

    def send(self, request, **kwargs):
        self.calls.append(kwargs)
        return self.handler(request)

    def close(self) -> None:
        pass
