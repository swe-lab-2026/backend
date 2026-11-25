from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_active_user
from app.db.models import SupportCaseStatus, SupportCaseType, SupportCategory, User
from app.schemas.support import (
    SupportActivityPage,
    SupportAssignableUser,
    SupportAssignmentUpdate,
    SupportCaseCreate,
    SupportCasePage,
    SupportCaseResponse,
    SupportCaseStatusValue,
    SupportCaseUpdate,
    SupportEscalationRequest,
    SupportMessageCreate,
    SupportMessagePage,
    SupportMessageResponse,
    SupportStatusUpdate,
)
from app.services import support as service
from app.services.analytics import as_utc_param

router = APIRouter(prefix="/support/cases", tags=["support"])


@router.post("", response_model=SupportCaseResponse, status_code=201)
async def create_support_case(
    payload: SupportCaseCreate,
    user: User = Depends(require_active_user),
    db: AsyncSession = Depends(get_db),
) -> SupportCaseResponse:
    """Open a case. Event/order/ticket context is resolved and verified server-side."""
    case, access = await service.create_case(db, user, payload)
    return await service.build_case_response(db, case, access)


@router.get("", response_model=SupportCasePage)
async def list_support_cases(
    status: list[SupportCaseStatusValue] | None = Query(default=None),
    category: SupportCategory | None = None,
    case_type: SupportCaseType | None = None,
    event_id: UUID | None = None,
    escalated: bool | None = None,
    assigned_to_me: bool = False,
    role: Literal["requester", "handler"] | None = None,
    scope: Literal["mine", "event", "all"] | None = None,
    q: str | None = Query(default=None, min_length=1, max_length=100),
    sort: Literal["-updated_at", "updated_at", "-created_at", "created_at"] = "-updated_at",
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(require_active_user),
    db: AsyncSession = Depends(get_db),
) -> SupportCasePage:
    """Only cases the caller may access are ever returned or counted.

    ``scope``: ``mine`` (cases I opened), ``event`` (attendee cases of events I
    own or staff), ``all`` (Platform Admins: platform and escalated cases).
    """
    return await service.list_cases(
        db,
        user,
        status=[SupportCaseStatus(s.value) for s in status] if status else None,
        category=category.value if category else None,
        case_type=case_type.value if case_type else None,
        event_id=event_id,
        escalated=escalated,
        assigned_to_me=assigned_to_me,
        role=role,
        scope=scope,
        q=q,
        sort=sort,
        limit=limit,
        offset=offset,
    )


@router.get("/{case_id}", response_model=SupportCaseResponse)
async def get_support_case(
    case_id: UUID,
    user: User = Depends(require_active_user),
    db: AsyncSession = Depends(get_db),
) -> SupportCaseResponse:
    case, access = await service.get_case(db, user, case_id)
    return await service.build_case_response(db, case, access)


@router.get("/{case_id}/messages", response_model=SupportMessagePage)
async def list_support_messages(
    case_id: UUID,
    after: UUID | None = Query(default=None, description="Return messages after this message id"),
    after_sequence: int | None = Query(default=None, ge=0),
    before_sequence: int | None = Query(default=None, ge=1),
    since: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    user: User = Depends(require_active_user),
    db: AsyncSession = Depends(get_db),
) -> SupportMessagePage:
    """Conversation history, oldest first.

    Polling: pass the id of the newest message you have as ``after`` (or the
    previous ``next_cursor`` as ``after_sequence``, or a timestamp as
    ``since``) to receive only newer messages.
    """
    return await service.list_messages(
        db,
        user,
        case_id,
        after_sequence=after_sequence,
        before_sequence=before_sequence,
        since=as_utc_param(since),
        limit=limit,
        after_message_id=after,
    )


@router.post("/{case_id}/messages", response_model=SupportMessageResponse, status_code=201)
async def send_support_message(
    case_id: UUID,
    payload: SupportMessageCreate,
    user: User = Depends(require_active_user),
    db: AsyncSession = Depends(get_db),
) -> SupportMessageResponse:
    return await service.post_message(db, user, case_id, payload.body)


@router.patch("/{case_id}", response_model=SupportCaseResponse)
async def update_support_case(
    case_id: UUID,
    payload: SupportCaseUpdate,
    user: User = Depends(require_active_user),
    db: AsyncSession = Depends(get_db),
) -> SupportCaseResponse:
    """Change status and/or assignee in one call. Omitted fields are unchanged;
    ``"assignee_user_id": null`` unassigns."""
    case, access = await service.update_case(
        db, user, case_id,
        status=SupportCaseStatus(payload.status.value) if payload.status else None,
        assignee_user_id=payload.assignee_user_id,
        assignee_given="assignee_user_id" in payload.model_fields_set,
        note=payload.note,
    )
    return await service.build_case_response(db, case, access)


@router.patch("/{case_id}/status", response_model=SupportCaseResponse)
async def update_support_case_status(
    case_id: UUID,
    payload: SupportStatusUpdate,
    user: User = Depends(require_active_user),
    db: AsyncSession = Depends(get_db),
) -> SupportCaseResponse:
    case, access = await service.update_status(
        db, user, case_id, SupportCaseStatus(payload.status.value), payload.note
    )
    return await service.build_case_response(db, case, access)


@router.patch("/{case_id}/assignee", response_model=SupportCaseResponse)
async def assign_support_case(
    case_id: UUID,
    payload: SupportAssignmentUpdate,
    user: User = Depends(require_active_user),
    db: AsyncSession = Depends(get_db),
) -> SupportCaseResponse:
    case, access = await service.assign_case(db, user, case_id, payload.assignee_user_id)
    return await service.build_case_response(db, case, access)


@router.get("/{case_id}/assignees", response_model=list[SupportAssignableUser])
@router.get("/{case_id}/assignable-users", response_model=list[SupportAssignableUser])
async def list_assignable_users(
    case_id: UUID,
    user: User = Depends(require_active_user),
    db: AsyncSession = Depends(get_db),
) -> list[SupportAssignableUser]:
    return await service.list_assignable(db, user, case_id)


@router.post("/{case_id}/escalate", response_model=SupportCaseResponse)
async def escalate_support_case(
    case_id: UUID,
    payload: SupportEscalationRequest,
    user: User = Depends(require_active_user),
    db: AsyncSession = Depends(get_db),
) -> SupportCaseResponse:
    case, access = await service.escalate_case(db, user, case_id, payload.reason)
    return await service.build_case_response(db, case, access)


@router.get("/{case_id}/activity", response_model=SupportActivityPage)
async def list_support_case_activity(
    case_id: UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(require_active_user),
    db: AsyncSession = Depends(get_db),
) -> SupportActivityPage:
    """Chronological lifecycle history (creation, status, assignment, escalation)."""
    return await service.list_activity(db, user, case_id, limit=limit, offset=offset)
