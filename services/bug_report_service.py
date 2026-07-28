# INFO: Reads and writes bug reports in Supabase.

from supabase import Client

from core.exceptions import NotFound, ResourceEmpty, UpstreamError
from core.upstream import translate_upstream_errors
from models.bug_reports import BugReport, BugReportList, CreateBugReportRequest

_COLUMNS = (
    "id, reporter_id, category, description, entity_type, entity_id, "
    "status, created_at, updated_at"
)


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


def list_my_bug_reports(db: Client, reporter_id: str) -> BugReportList:
    with translate_upstream_errors():
        response = (
            db.table("bug_reports")
            .select(_COLUMNS)
            .eq("reporter_id", reporter_id)
            .order("created_at", desc=True)
            .execute()
        )

        if not response.data:
            raise ResourceEmpty("no_bug_reports")

        return BugReportList(bug_reports=[BugReport(**row) for row in response.data])


def list_bug_reports(db: Client) -> BugReportList:
    with translate_upstream_errors():
        response = (
            db.table("bug_reports")
            .select(_COLUMNS)
            .order("created_at", desc=True)
            .execute()
        )

        if not response.data:
            raise ResourceEmpty("no_bug_reports")

        return BugReportList(bug_reports=[BugReport(**row) for row in response.data])


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
