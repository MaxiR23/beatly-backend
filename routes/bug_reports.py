# INFO: Bug reports endpoints.

from uuid import UUID

from fastapi import APIRouter, Depends
from supabase import Client

from core.auth import get_current_user_id, require_role
from core.database import get_db
from models.bug_reports import (
    BugReport,
    BugReportList,
    CreateBugReportRequest,
    UpdateBugReportStatusRequest,
)
from models.profiles import Profile, Role
from models.responses import ApiSuccess, ok_response
from services.bug_report_service import (
    create_bug_report,
    list_bug_reports,
    list_my_bug_reports,
    update_bug_report_status,
)

router = APIRouter(prefix="/bug-reports", tags=["bug-reports"])


@router.post("", response_model=ApiSuccess[BugReport])
def create_bug_report_route(
    payload: CreateBugReportRequest,
    user_id: str = Depends(get_current_user_id),
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[BugReport]:
    return ok_response(create_bug_report(db, user_id, payload))


@router.get("/me", response_model=ApiSuccess[BugReportList])
def list_my_bug_reports_route(
    user_id: str = Depends(get_current_user_id),
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[BugReportList]:
    return ok_response(list_my_bug_reports(db, user_id))


@router.get("", response_model=ApiSuccess[BugReportList])
def list_bug_reports_route(
    profile: Profile = Depends(require_role(Role.ADMIN)),  # noqa: B008
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[BugReportList]:
    return ok_response(list_bug_reports(db))


@router.patch("/{report_id}", response_model=ApiSuccess[BugReport])
def update_bug_report_status_route(
    report_id: UUID,
    payload: UpdateBugReportStatusRequest,
    profile: Profile = Depends(require_role(Role.ADMIN)),  # noqa: B008
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[BugReport]:
    return ok_response(update_bug_report_status(db, str(report_id), payload.status))
