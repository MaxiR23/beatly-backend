# INFO: Reads and writes bug reports in Supabase.

from supabase import Client

from core.exceptions import NotFound, UpstreamError
from core.pagination import PageRequest, SortKey, ValueType, apply_page, build_page
from core.upstream import translate_upstream_errors
from models.bug_reports import BugReport, CreateBugReportRequest
from models.responses import PageBlock

_COLUMNS = (
    "id, reporter_id, category, description, entity_type, entity_id, "
    "status, created_at, updated_at"
)

# The same key for both GET /bug-reports/me and GET /bug-reports: the two
# endpoints ask for the same order ("newest first"), unlike _LIST_SORT and
# _SYNC_SORT in services/likes_service.py, which sort on different columns
# for two endpoints with different orders. created_at and not updated_at:
# created_at is immutable, while updated_at moves on every status PATCH, so
# ordering by it would move a report mid-walk. Neither id_column nor id_type
# is declared: SortKey's defaults (id_column="id", id_type=ValueType.UUID)
# already match bug_reports.id.
_LIST_SORT = SortKey("created_at", ValueType.TIMESTAMP)


def create_bug_report(
    db: Client, reporter_id: str, payload: CreateBugReportRequest
) -> BugReport:
    with translate_upstream_errors():
        insert_payload = {
            **payload.model_dump(),
            "reporter_id": reporter_id,
            "status": "open",
        }
        response = db.table("bug_reports").insert(insert_payload).execute()

        # An insert returning no row is an upstream anomaly. Indexing
        # blindly would raise IndexError, which is not translated, and
        # surface as a 500.
        if not response.data:
            raise UpstreamError()

        return BugReport(**response.data[0])


def list_my_bug_reports(
    db: Client, reporter_id: str, page: PageRequest
) -> tuple[list[BugReport], PageBlock]:
    with translate_upstream_errors():
        # Decoded here, ahead of any db.table() call, so a bad cursor never
        # reaches the database — apply_page decodes it again below to build
        # the filter, but by then it is already known to be valid.
        page.decode(_LIST_SORT)

        query = (
            db.table("bug_reports")
            .select(_COLUMNS, count=page.count_mode)
            .eq("reporter_id", reporter_id)
        )
        query = apply_page(query, _LIST_SORT, page)

        response = query.execute()

        rows, block = build_page(response.data or [], page, _LIST_SORT, response.count)
        return [BugReport(**row) for row in rows], block


def list_bug_reports(
    db: Client, page: PageRequest
) -> tuple[list[BugReport], PageBlock]:
    with translate_upstream_errors():
        # Decoded here, ahead of any db.table() call, for the same reason as
        # list_my_bug_reports above.
        page.decode(_LIST_SORT)

        # Deliberately unscoped: this is the admin's triage listing, so it
        # returns reports from every reporter, unlike list_my_bug_reports.
        query = db.table("bug_reports").select(_COLUMNS, count=page.count_mode)
        query = apply_page(query, _LIST_SORT, page)

        response = query.execute()

        rows, block = build_page(response.data or [], page, _LIST_SORT, response.count)
        return [BugReport(**row) for row in rows], block


def update_bug_report_status(db: Client, report_id: str, status: str) -> BugReport:
    with translate_upstream_errors():
        response = (
            db.table("bug_reports")
            .update({"status": status})
            .eq("id", report_id)
            .select(_COLUMNS)
            .execute()
        )

        if not response.data:
            raise NotFound("report_not_found")

        return BugReport(**response.data[0])
