from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.errors import ApiError
from app.db.models import Notification, User
from app.schemas.admin import EventReportResponse, Page
from app.schemas.support import NotificationPage, NotificationResponse
from app.schemas.users import UpdateUserRequest, UserResponse
from app.services import notifications
from app.services import reports as report_service

router = APIRouter(prefix="/users", tags=["users"])


def _to_response(user: User) -> UserResponse:
    return UserResponse(
        user_id=user.id,
        email=user.email,
        first_name=user.first_name,
        last_name=user.last_name,
        global_role=user.global_role.value,
        status=user.status.value,
        email_verified_at=user.email_verified_at,
        created_at=user.created_at,
        locale=user.locale,
    )


@router.get("/me", response_model=UserResponse)
async def get_me(user: User = Depends(get_current_user)) -> UserResponse:
    return _to_response(user)


@router.patch("/me", response_model=UserResponse)
async def update_me(
    body: UpdateUserRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    if body.first_name is not None:
        user.first_name = body.first_name.strip()
    if body.last_name is not None:
        user.last_name = body.last_name.strip()
    if body.locale is not None:
        user.locale = body.locale
    return _to_response(user)


def _notification_response(row: Notification) -> NotificationResponse:
    return NotificationResponse(
        id=row.id,
        type=row.type,
        channel=row.channel.value,
        payload=row.payload or {},
        status=row.status.value,
        created_at=row.created_at,
        read_at=row.read_at,
    )


@router.get("/me/notifications", response_model=NotificationPage)
async def list_my_notifications(
    unread_only: bool = False,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NotificationPage:
    """In-app notifications (support cases, assignments, escalations), newest first."""
    rows, total, unread = await notifications.list_user_notifications(
        db, user.id, unread_only=unread_only, limit=limit, offset=offset
    )
    return NotificationPage(
        items=[_notification_response(row) for row in rows],
        total=total,
        unread_count=unread,
        limit=limit,
        offset=offset,
    )


@router.post("/me/notifications/{notification_id}/read", response_model=NotificationResponse)
async def mark_my_notification_read(
    notification_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NotificationResponse:
    row = await notifications.mark_notification_read(db, user.id, notification_id)
    if row is None:
        raise ApiError(404, "NOTIFICATION_NOT_FOUND", "Notification not found")
    return _notification_response(row)


@router.get("/me/reports", response_model=Page[EventReportResponse])
async def list_my_reports(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Page[EventReportResponse]:
    """Event reports the caller has submitted, newest first."""
    return await report_service.list_my_reports(db, user, limit=limit, offset=offset)
