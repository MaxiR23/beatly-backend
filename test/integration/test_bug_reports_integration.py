# test/integration/test_bug_reports_integration.py
#
# Integration tests of the bug reports endpoints against the local stack.
#
# Tested:
# - POST /bug-reports stores a report for the caller
# - GET /bug-reports/me lists the caller's reports
# - GET /bug-reports (admin) lists reports, by membership
# - PATCH /bug-reports/{report_id} (admin) closes a report
# - GET /bug-reports (admin) carries each reporter's own profile across
#   two cursor pages, and null names for a reporter with none set
# - POST, /me and PATCH do not return reporter
#
# What is covered:
# - The queries and RPCs of these routes against the real schema, with RLS
#   running as the caller
#
# Run with: pytest -m integration test/integration/test_bug_reports_integration.py -v
#
# SEE: routes/bug_reports.py, services/bug_report_service.py

from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration

_BODY = {"category": "ui", "description": "The button does not respond"}


def _create(client, user):
    response = client.post("/bug-reports", json=_BODY, headers=user.headers)
    assert response.status_code == 200
    return response.json()["data"]


def _set_profile(admin, user, display_name, username):
    admin.table("profiles").update(
        {"display_name": display_name, "username": username}
    ).eq("id", user.user_id).execute()


def _username():
    return f"it_{uuid4().hex[:12]}"


def test_post_bug_report_stores_a_report_for_the_caller(client, make_user, admin):
    user = make_user()

    response = client.post("/bug-reports", json=_BODY, headers=user.headers)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["reporter_id"] == user.user_id
    assert body["data"]["status"] == "open"
    assert "reporter" not in body["data"]
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
    assert all("reporter" not in item for item in body["data"]["items"])


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
    assert "reporter" not in response.json()["data"]
    stored = (
        admin.table("bug_reports").select("status").eq("id", created["id"]).execute()
    )
    assert stored.data == [{"status": "closed"}]


def test_get_bug_reports_as_admin_carries_each_reporters_profile_across_pages(
    client, make_user, admin
):
    reporter_a = make_user()
    reporter_b = make_user()
    admin_user = make_user(role="admin")
    profiles = {
        reporter_a.user_id: ("Reporter A", _username()),
        reporter_b.user_id: ("Reporter B", _username()),
    }
    for user, (name, username) in (
        (reporter_a, profiles[reporter_a.user_id]),
        (reporter_b, profiles[reporter_b.user_id]),
        (admin_user, ("Admin Caller", _username())),
    ):
        _set_profile(admin, user, name, username)
    report_a = _create(client, reporter_a)
    report_b = _create(client, reporter_b)

    first = client.get("/bug-reports", params={"limit": 1}, headers=admin_user.headers)
    assert first.status_code == 200
    first_data = first.json()["data"]
    assert first_data["page"]["total"] is not None
    cursor = first_data["page"]["next_cursor"]
    assert cursor is not None

    second = client.get(
        "/bug-reports",
        params={"limit": 1, "cursor": cursor},
        headers=admin_user.headers,
    )
    assert second.status_code == 200
    second_data = second.json()["data"]
    assert second_data["page"]["total"] is None

    items = first_data["items"] + second_data["items"]
    assert {item["id"] for item in items} == {report_a["id"], report_b["id"]}
    for item in items:
        name, username = profiles[item["reporter_id"]]
        assert item["reporter"] == {
            "id": item["reporter_id"],
            "display_name": name,
            "username": username,
        }


def test_get_bug_reports_reporter_without_names_returns_nulls(client, make_user):
    reporter = make_user()
    admin_user = make_user(role="admin")
    created = _create(client, reporter)

    response = client.get(
        "/bug-reports", params={"limit": 100}, headers=admin_user.headers
    )

    assert response.status_code == 200
    item = next(i for i in response.json()["data"]["items"] if i["id"] == created["id"])
    assert item["reporter"] == {
        "id": reporter.user_id,
        "display_name": None,
        "username": None,
    }
