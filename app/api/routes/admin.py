"""Platform Admin API (SCRUM-17). Every route requires PLATFORM_ADMIN.

The role is checked per request against the live user row (an access token
issued before a demotion or suspension stops working immediately).
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_roles
from app.db.models import (
    EventStatus,
    OrderStatus,
    PaymentStatus,
    RefundStatus,
    SupportCaseStatus,
    SupportCaseType,
    User,
    UserRole,
    UserStatus,
)
from app.schemas.admin import (
    AdminActionResponse,
    AdminActivationResponse,
    AdminEventActionResponse,
    AdminEventResponse,
    AdminOrderResponse,
    AdminPaymentResponse,
    AdminReasonRequest,
    AdminRefundResponse,
    AdminUserResponse,
    DisputeCreate,
    DisputeResponse,
    DisputeStatusValue,
    DisputeUpdate,
    FailureKind,
    OperationalFailureResponse,
    Page,
)
from app.schemas.support import SupportCasePage, SupportCaseStatusValue
from app.services import admin as service
from app.services import support as support_service

require_admin = require_roles("PLATFORM_ADMIN")

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/example")
async def admin_example(user: User = Depends(require_admin)) -> dict[str, object]:
    return {"ok": True, "user_id": str(user.id)}


# ---------- Users ----------


@router.get("/users", response_model=Page[AdminUserResponse])
async def search_users(
    q: str | None = Query(default=None, min_length=1, max_length=200),
    status: UserStatus | None = None,
    role: UserRole | None = None,
    is_organizer: bool | None = None,
    sort: Literal["-created_at", "created_at", "email", "-email", "last_name", "-last_name"] = (
        "-created_at"
    ),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> Page[AdminUserResponse]:
    """Search by email, name or exact user id."""
    return await service.search_users(
        db, q=q, status=status, role=role, is_organizer=is_organizer,
        sort=sort, limit=limit, offset=offset,
    )


@router.get("/users/{user_id}", response_model=AdminUserResponse)
async def get_user(user_id: UUID, db: AsyncSession = Depends(get_db)) -> AdminUserResponse:
    return await service.get_user(db, user_id)


@router.post("/users/{user_id}/suspend", response_model=AdminUserResponse)
async def suspend_user(
    user_id: UUID,
    payload: AdminReasonRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminUserResponse:
    """Block the account. Refresh sessions are revoked and every existing access
    token is rejected from the next request on."""
    return await service.suspend_user(db, admin, user_id, payload.reason)


@router.post("/users/{user_id}/unsuspend", response_model=AdminUserResponse)
async def unsuspend_user(
    user_id: UUID,
    payload: AdminReasonRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminUserResponse:
    return await service.unsuspend_user(db, admin, user_id, payload.reason)


# ---------- Events ----------


@router.get("/events", response_model=Page[AdminEventResponse])
async def search_events(
    q: str | None = Query(default=None, min_length=1, max_length=200),
    status: EventStatus | None = None,
    suspended: bool | None = None,
    owner_user_id: UUID | None = None,
    starts_from: datetime | None = None,
    starts_to: datetime | None = None,
    sort: Literal["-created_at", "created_at", "starts_at", "-starts_at", "title", "-title"] = (
        "-created_at"
    ),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> Page[AdminEventResponse]:
    return await service.search_events(
        db, q=q, status=status, suspended=suspended, owner_user_id=owner_user_id,
        starts_from=starts_from, starts_to=starts_to, sort=sort, limit=limit, offset=offset,
    )


@router.get("/events/{event_id}", response_model=AdminEventResponse)
async def get_event(event_id: UUID, db: AsyncSession = Depends(get_db)) -> AdminEventResponse:
    return await service.get_event(db, event_id)


@router.post("/events/{event_id}/suspend", response_model=AdminEventActionResponse)
async def suspend_event(
    event_id: UUID,
    payload: AdminReasonRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminEventActionResponse:
    """Stop all new sales and hide the event publicly. Existing orders, tickets
    and refunds keep their normal lifecycle; pending checkouts cannot complete."""
    return AdminEventActionResponse(
        **await service.suspend_event(db, admin, event_id, payload.reason)
    )


@router.post("/events/{event_id}/unsuspend", response_model=AdminEventActionResponse)
async def unsuspend_event(
    event_id: UUID,
    payload: AdminReasonRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminEventActionResponse:
    return AdminEventActionResponse(
        **await service.unsuspend_event(db, admin, event_id, payload.reason)
    )


@router.post("/events/{event_id}/paid-sales/stop", response_model=AdminEventActionResponse)
async def stop_paid_sales(
    event_id: UUID,
    payload: AdminReasonRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminEventActionResponse:
    """Stop paid checkout only; free registration stays available."""
    return AdminEventActionResponse(
        **await service.stop_paid_sales(db, admin, event_id, payload.reason)
    )


@router.post("/events/{event_id}/paid-sales/resume", response_model=AdminEventActionResponse)
async def resume_paid_sales(
    event_id: UUID,
    payload: AdminReasonRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminEventActionResponse:
    return AdminEventActionResponse(
        **await service.resume_paid_sales(db, admin, event_id, payload.reason)
    )


# ---------- Orders and payments ----------


@router.get("/orders", response_model=Page[AdminOrderResponse])
async def search_orders(
    q: str | None = Query(default=None, min_length=1, max_length=200),
    status: OrderStatus | None = None,
    event_id: UUID | None = None,
    buyer_user_id: UUID | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    sort: Literal[
        "-created_at", "created_at", "-total_amount", "total_amount", "-paid_at", "paid_at"
    ] = "-created_at",
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> Page[AdminOrderResponse]:
    """Search by order number, buyer email or exact order id."""
    return await service.search_orders(
        db, q=q, status=status, event_id=event_id, buyer_user_id=buyer_user_id,
        created_from=created_from, created_to=created_to, sort=sort, limit=limit, offset=offset,
    )


@router.get("/payments", response_model=Page[AdminPaymentResponse])
async def search_payments(
    q: str | None = Query(default=None, min_length=1, max_length=200),
    status: PaymentStatus | None = None,
    provider: str | None = Query(default=None, max_length=50),
    order_id: UUID | None = None,
    event_id: UUID | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    sort: Literal["-created_at", "created_at", "-amount", "amount"] = "-created_at",
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> Page[AdminPaymentResponse]:
    """Search by provider reference, order number, or exact payment/order id."""
    return await service.search_payments(
        db, q=q, status=status, provider=provider, order_id=order_id, event_id=event_id,
        created_from=created_from, created_to=created_to, sort=sort, limit=limit, offset=offset,
    )


# ---------- Monitoring ----------


@router.get("/activations", response_model=Page[AdminActivationResponse])
async def list_activations(
    status: Literal["NOT_ACTIVATED", "ACTIVE", "SUSPENDED"] | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> Page[AdminActivationResponse]:
    """Per-event paid-sales activation records."""
    return await service.list_activations(db, status=status, limit=limit, offset=offset)


@router.get("/refunds", response_model=Page[AdminRefundResponse])
async def list_refunds(
    status: RefundStatus | None = None,
    kind: Literal["CUSTOMER_REQUEST", "EVENT_CANCELLATION"] | None = None,
    event_id: UUID | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> Page[AdminRefundResponse]:
    return await service.list_refunds(
        db, status=status, kind=kind, event_id=event_id,
        created_from=created_from, created_to=created_to, limit=limit, offset=offset,
    )


@router.get("/failures", response_model=Page[OperationalFailureResponse])
async def list_failures(
    kind: FailureKind | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> Page[OperationalFailureResponse]:
    """Failed payments, failed refunds and failed notification emails."""
    return await service.list_failures(
        db, kind=kind.value if kind else None, created_from=created_from,
        created_to=created_to, limit=limit, offset=offset,
    )


@router.get("/support/escalated", response_model=SupportCasePage)
async def list_escalated_support_cases(
    status: list[SupportCaseStatusValue] | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SupportCasePage:
    """Attendee cases explicitly escalated to Platform Admins (SCRUM-16)."""
    return await support_service.list_cases(
        db, admin,
        status=[SupportCaseStatus(s.value) for s in status] if status else None,
        case_type=SupportCaseType.ATTENDEE_ORGANIZER.value,
        escalated=True,
        sort="-updated_at",
        limit=limit,
        offset=offset,
    )


@router.get("/actions", response_model=Page[AdminActionResponse])
async def list_admin_actions(
    action: str | None = Query(default=None, max_length=100),
    entity_type: str | None = Query(default=None, max_length=100),
    entity_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> Page[AdminActionResponse]:
    """Administrative actions from the append-only audit log."""
    return await service.list_admin_actions(
        db, action=action, entity_type=entity_type, entity_id=entity_id,
        limit=limit, offset=offset,
    )


# ---------- Disputes ----------


@router.get("/disputes", response_model=Page[DisputeResponse])
async def list_disputes(
    status: DisputeStatusValue | None = None,
    event_id: UUID | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> Page[DisputeResponse]:
    return await service.list_disputes(
        db, status=status.value if status else None, event_id=event_id,
        limit=limit, offset=offset,
    )


@router.post("/disputes", response_model=DisputeResponse, status_code=201)
async def open_dispute(
    payload: DisputeCreate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> DisputeResponse:
    """Record a dispute against a captured payment (bookkeeping only)."""
    return await service.open_dispute(
        db, admin, payment_id=payload.payment_id, reason=payload.reason,
        amount=payload.amount, support_case_id=payload.support_case_id,
    )


@router.patch("/disputes/{dispute_id}", response_model=DisputeResponse)
async def update_dispute(
    dispute_id: UUID,
    payload: DisputeUpdate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> DisputeResponse:
    return await service.update_dispute(
        db, admin, dispute_id, status=payload.status.value, note=payload.resolution_note
    )
