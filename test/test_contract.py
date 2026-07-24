# test/test_contract.py
#
# Tests for the global response contract and exception handlers.
#
# Tested:
# - A success response has ok:true and no "reason" key
# - An unknown route returns {"ok": false, "reason": "not_found"} with 404
# - A domain exception raised with a custom reason keeps that reason
#   and the status code defined by its class
# - A domain exception raised without a reason falls back to the
#   class default reason
# - A domain exception's status code comes from its own class, not
#   hardcoded to 404 (UpstreamError -> 502)
# - An unhandled exception returns {"ok": false, "reason":
#   "internal_error"} with 500, and never leaks the exception message
# - A request validation failure (bad query param type) returns
#   {"ok": false, "reason": "invalid_request"} with 422
#
# What is covered:
# - models/responses.py envelope shape, core/exceptions.py mapping,
#   and the four global handlers wired in app.py
#
# Run with: pytest test/test_contract.py -v
#
# SEE: app.py, core/exceptions.py, models/responses.py

from fastapi.testclient import TestClient

from app import app
from core.exceptions import NotFound, UpstreamError

client = TestClient(app, raise_server_exceptions=False)

_CUSTOM_REASON = "custom_reason_here"
_SECRET_MESSAGE = "leaked db password abc123"


@app.get("/_test/raises-custom-reason")
def _raise_custom_reason():
    raise NotFound(_CUSTOM_REASON)


@app.get("/_test/raises-default-reason")
def _raise_default_reason():
    raise NotFound()


@app.get("/_test/raises-unhandled")
def _raise_unhandled():
    raise ValueError(_SECRET_MESSAGE)


@app.get("/_test/raises-upstream-error")
def _raise_upstream_error():
    raise UpstreamError()


@app.get("/_test/requires-int-query")
def _requires_int_query(count: int):
    return {"count": count}


def test_success_response_omits_reason():
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert "reason" not in body


def test_unknown_route_returns_contract_shape():
    response = client.get("/this-route-does-not-exist")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "not_found"}


def test_domain_exception_with_custom_reason_keeps_reason_and_status():
    response = client.get("/_test/raises-custom-reason")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": _CUSTOM_REASON}


def test_domain_exception_without_reason_uses_class_default():
    response = client.get("/_test/raises-default-reason")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "not_found"}


def test_unhandled_exception_does_not_leak_details():
    response = client.get("/_test/raises-unhandled")

    assert response.status_code == 500
    assert response.json() == {"ok": False, "reason": "internal_error"}
    assert _SECRET_MESSAGE not in response.text


def test_domain_exception_status_code_comes_from_its_own_class():
    response = client.get("/_test/raises-upstream-error")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_invalid_query_param_returns_contract_shape():
    response = client.get("/_test/requires-int-query", params={"count": "not-a-number"})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}