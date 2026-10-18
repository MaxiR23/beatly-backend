# test/integration/test_bug_reports_integration.py
#
# Integration tests of the bug reports endpoints against the local stack.
#
# Tested:
# - POST /bug-reports stores a report for the caller
# - GET /bug-reports/me lists the caller's reports
# - GET /bug-reports (admin, developer) lists reports, by membership
# - PATCH /bug-reports/{report_id} (admin, developer) closes a report
# - GET /bug-reports as a developer includes another user's report with
#   its reporter's profile
# - PATCH /bug-reports/{report_id} as a developer closes and reopens a report
# - GET /bug-reports (admin) carries each reporter's own profile across
#   two cursor pages, and null names for a reporter with none set
# - POST, /me and PATCH do not return reporter
# - GET /bug-reports?status=open|closed walks only that status across pages,
#   with a total equal to a service-role count of the same filter
# - A cursor emitted under a status filter is 422 invalid_cursor under the
#   other filter, under none, and on GET /bug-reports/me
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


def test_get_bug_reports_as_developer_includes_another_users_report_with_its_reporter(
    client, make_user, admin
):
    reporter = make_user()
    username = _username()
    _set_profile(admin, reporter, "Reporter Dev", username)
    created = _create(client, reporter)
    developer = make_user(role="developer")

    response = client.get(
        "/bug-reports", params={"limit": 100}, headers=developer.headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    item = next(i for i in body["data"]["items"] if i["id"] == created["id"])
    assert item["reporter"] == {
        "id": reporter.user_id,
        "display_name": "Reporter Dev",
        "username": username,
    }


def test_patch_bug_report_as_developer_closes_and_reopens_the_report(
    client, make_user, admin
):
    reporter = make_user()
    created = _create(client, reporter)
    developer = make_user(role="developer")

    for status in ("closed", "open"):
        response = client.patch(
            f"/bug-reports/{created['id']}",
            json={"status": status},
            headers=developer.headers,
        )

        assert response.status_code == 200
        assert response.json()["data"]["status"] == status
        assert "reporter" not in response.json()["data"]
        stored = (
            admin.table("bug_reports")
            .select("status")
            .eq("id", created["id"])
            .execute()
        )
        assert stored.data == [{"status": status}]


def _walk(client, headers, status, limit):
    params = {"limit": limit}
    if status is not None:
        params["status"] = status
    items, totals, pages, cursor = [], [], 0, None
    while True:
        page_params = dict(params)
        if cursor is not None:
            page_params["cursor"] = cursor
        response = client.get("/bug-reports", params=page_params, headers=headers)
        assert response.status_code == 200
        data = response.json()["data"]
        items.extend(data["items"])
        totals.append(data["page"]["total"])
        pages += 1
        cursor = data["page"]["next_cursor"]
        if cursor is None:
            return items, totals, pages


def test_get_bug_reports_filtered_by_status_walks_only_that_status_across_pages(
    client, make_user, admin
):
    reporter = make_user()
    admin_user = make_user(role="admin")
    created = [_create(client, reporter)["id"] for _ in range(6)]
    closed_ids = created[:3]
    open_ids = created[3:]
    admin.table("bug_reports").update({"status": "closed"}).in_(
        "id", closed_ids
    ).execute()

    expected = {"open": open_ids, "closed": closed_ids}
    other = {"open": closed_ids, "closed": open_ids}
    totals_by_status = {}
    for status in ("open", "closed"):
        items, totals, pages = _walk(client, admin_user.headers, status, 2)
        ids = [item["id"] for item in items]
        stored_total = (
            admin.table("bug_reports")
            .select("id", count="exact")
            .eq("status", status)
            .execute()
            .count
        )
        totals_by_status[status] = stored_total

        assert all(item["status"] == status for item in items)
        assert len(ids) == len(set(ids))
        assert pages >= 2
        assert set(expected[status]) <= set(ids)
        assert not set(other[status]) & set(ids)
        assert totals[0] == stored_total
        assert all(total is None for total in totals[1:])
        assert len(ids) == stored_total

    unfiltered = client.get(
        "/bug-reports", params={"limit": 1}, headers=admin_user.headers
    )
    assert unfiltered.status_code == 200
    assert unfiltered.json()["data"]["page"]["total"] == sum(totals_by_status.values())


def test_get_bug_reports_rejects_a_cursor_from_another_status_filter(client, make_user):
    reporter = make_user()
    admin_user = make_user(role="admin")
    _create(client, reporter)
    _create(client, reporter)

    first = client.get(
        "/bug-reports",
        params={"status": "open", "limit": 1},
        headers=admin_user.headers,
    )
    assert first.status_code == 200
    cursor = first.json()["data"]["page"]["next_cursor"]
    assert cursor is not None

    for path, params in (
        ("/bug-reports", {"status": "closed", "cursor": cursor}),
        ("/bug-reports", {"cursor": cursor}),
        ("/bug-reports/me", {"cursor": cursor}),
    ):
        response = client.get(path, params=params, headers=admin_user.headers)

        assert response.status_code == 422
        assert response.json() == {"ok": False, "reason": "invalid_cursor"}
