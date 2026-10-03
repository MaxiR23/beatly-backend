# test/integration/test_bug_reports_integration.py
#
# Integration tests of the bug reports endpoints against the local stack.
#
# Tested:
# - POST /bug-reports stores a report for the caller
# - GET /bug-reports/me lists the caller's reports
# - GET /bug-reports (admin) lists reports, by membership
# - PATCH /bug-reports/{report_id} (admin) closes a report
#
# What is covered:
# - The queries and RPCs of these routes against the real schema, with RLS
#   running as the caller
#
# Run with: pytest -m integration test/integration/test_bug_reports_integration.py -v
#
# SEE: routes/bug_reports.py, services/bug_report_service.py

import pytest

pytestmark = pytest.mark.integration

_BODY = {"category": "ui", "description": "The button does not respond"}


def _create(client, user):
    response = client.post("/bug-reports", json=_BODY, headers=user.headers)
    assert response.status_code == 200
    return response.json()["data"]


def test_post_bug_report_stores_a_report_for_the_caller(client, make_user, admin):
    user = make_user()

    response = client.post("/bug-reports", json=_BODY, headers=user.headers)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["reporter_id"] == user.user_id
    assert body["data"]["status"] == "open"
    stored = (
        admin.table("bug_reports")
        .select("description")
        .eq("id", body["data"]["id"])
        .execute()
        .data
    )
    assert stored == [{"description": _BODY["description"]}]


def test_get_my_bug_reports_lists_the_callers_reports(client, make_user):
    user = make_user()
    created = _create(client, user)

    response = client.get("/bug-reports/me", headers=user.headers)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert [item["id"] for item in body["data"]["items"]] == [created["id"]]


def test_get_bug_reports_as_admin_includes_a_report_of_another_user(client, make_user):
    reporter = make_user()
    created = _create(client, reporter)
    admin_user = make_user(role="admin")

    response = client.get(
        "/bug-reports", params={"limit": 100}, headers=admin_user.headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert created["id"] in [item["id"] for item in body["data"]["items"]]


def test_patch_bug_report_as_admin_closes_the_report(client, make_user, admin):
    reporter = make_user()
    created = _create(client, reporter)
    admin_user = make_user(role="admin")

    response = client.patch(
        f"/bug-reports/{created['id']}",
        json={"status": "closed"},
        headers=admin_user.headers,
    )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "closed"
    stored = (
        admin.table("bug_reports").select("status").eq("id", created["id"]).execute()
    )
    assert stored.data == [{"status": "closed"}]
