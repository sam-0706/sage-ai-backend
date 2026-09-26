"""Faculty & advisor workspace: only assigned or student-shared cases (PRD Epic 7)."""
from uuid import UUID

from fastapi import APIRouter, Depends

from app.auth.deps import Principal, require_staff
from app.domain.schemas import CaseNoteIn
from app.repositories.db import transaction
from app.services import plans as plans_svc

router = APIRouter(prefix="/staff", tags=["staff"])


@router.get("/cases")
async def list_cases(status: str | None = None, p: Principal = Depends(require_staff)):
    async with transaction() as conn:
        items = await plans_svc.list_cases(conn, p, status)
    return {"items": items,
            "empty_state": None if items else "No students are connected to you yet. Cases appear here only when a student "
                                              "shares a plan or a case is assigned to you. You can preview the advisor templates."}


@router.get("/cases/{case_id}")
async def get_case(case_id: UUID, p: Principal = Depends(require_staff)):
    async with transaction() as conn:
        return await plans_svc.get_case(conn, p, case_id)


@router.post("/cases/{case_id}/notes", status_code=201, summary="Add a note, change status, or record a follow-up")
async def add_note(case_id: UUID, body: CaseNoteIn, p: Principal = Depends(require_staff)):
    async with transaction() as conn:
        return await plans_svc.add_case_note(conn, p, case_id, body.kind, body.body, body.status, body.follow_up_at)
