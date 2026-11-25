"""Platform Admin API (SCRUM-17). Every route requires PLATFORM_ADMIN.

The role is checked per request against the live user row (an access token
issued before a demotion or suspension stops working immediately).
"""

import csv
import io
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_roles
from app.api.routes.organizers import _payout_response, _profile_response
from app.core.errors import ApiError
from app.db import repositories as repo
from app.db.models import (
    EventStatus,
    OrderStatus,
    PaymentStatus,
    PayoutAccount,
    PayoutStatus,
    PlatformSettings,
    RefundStatus,
    SupportCaseStatus,
    SupportCaseType,
    User,
    UserRole,
    UserStatus,
    VerificationStatus,
)
from app.schemas.admin import (
    AdminActionResponse,
    AdminActivationResponse,
    AdminCampaignResponse,
    AdminEventActionResponse,
    AdminEventReportResponse,
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
    EventReportReasonValue,
    EventReportStatusValue,
    EventReportUpdate,
    FailureKind,
    OperationalFailureResponse,
    OrganizerVerificationUpdate,
    Page,
    PayoutStatusUpdate,
    PlatformSettingsResponse,
    PlatformSettingsUpdate,
)
from app.schemas.organizers import OrganizerProfileResponse, PayoutAccountResponse
from app.schemas.support import SupportCasePage, SupportCaseStatusValue
from app.services import admin as service
from app.services import organizers as organizer_service
from app.services import platform_settings
from app.services import reports as report_service
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
    q: str | None = Query(default=None, min_length=1, max_length=200),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> Page[AdminActivationResponse]:
    """Per-event paid-sales activation records."""
    return await service.list_activations(db, status=status, q=q, limit=limit, offset=offset)


@router.get("/refunds", response_model=Page[AdminRefundResponse])
async def list_refunds(
    status: RefundStatus | None = None,
    kind: Literal["CUSTOMER_REQUEST", "EVENT_CANCELLATION", "ORGANIZER_REQUEST"] | None = None,
    q: str | None = Query(default=None, min_length=1, max_length=200),
    event_id: UUID | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> Page[AdminRefundResponse]:
    return await service.list_refunds(
        db, status=status, kind=kind, event_id=event_id, q=q,
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


@router.get("/campaigns", response_model=Page[AdminCampaignResponse])
async def list_campaigns(
    q: str | None = Query(default=None, min_length=1, max_length=200),
    event_id: UUID | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> Page[AdminCampaignResponse]:
    """All promo campaigns; ``q`` matches name, code or event title."""
    return await service.list_campaigns(db, q=q, event_id=event_id, limit=limit, offset=offset)


@router.get(
    "/export/{kind}.csv",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}}}},
)
async def export_csv(
    kind: Literal["users", "events", "orders", "payments"],
    q: str | None = Query(default=None, min_length=1, max_length=200),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """CSV export (newest first, at most 10,000 rows) with the same ``q`` search
    and redaction as the JSON lists. Formula-like cells are prefixed with '."""
    rows = await service.export_rows(db, kind, q=q)
    buffer = io.StringIO()
    csv.writer(buffer).writerows(rows)
    await repo.add_audit_entry(
        db, actor_user_id=admin.id, actor_type="PLATFORM_ADMIN", entity_type="export",
        entity_id=None, action="admin.data_exported",
        metadata={"kind": kind, "q": q, "rows": len(rows) - 1},
    )
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{kind}.csv"'},
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


# ---------- Organizer verification and payout approval ----------


@router.post("/organizers/{user_id}/verification", response_model=OrganizerProfileResponse)
async def set_organizer_verification(
    user_id: UUID,
    payload: OrganizerVerificationUpdate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> OrganizerProfileResponse:
    profile = await repo.get_organizer_profile(db, user_id)
    if profile is None:
        raise ApiError(404, "ORGANIZER_PROFILE_NOT_FOUND", "Organizer profile not found")
    await organizer_service.set_verification_status(
        db, admin, profile, VerificationStatus(payload.status),
        action="admin.organizer_verification_set", actor_type="PLATFORM_ADMIN",
        reason=payload.reason,
    )
    return _profile_response(profile)


@router.post("/payout-accounts/{account_id}/status", response_model=PayoutAccountResponse)
async def set_payout_account_status(
    account_id: UUID,
    payload: PayoutStatusUpdate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> PayoutAccountResponse:
    account = await db.get(PayoutAccount, account_id)
    if account is None:
        raise ApiError(404, "PAYOUT_ACCOUNT_NOT_FOUND", "Payout account not found")
    await organizer_service.set_payout_status(
        db, admin, account, PayoutStatus(payload.status),
        action="admin.payout_account_status_set", actor_type="PLATFORM_ADMIN",
        reason=payload.reason,
    )
    return _payout_response(account)


# ---------- Reported events (SRS 4.12) ----------


@router.get("/reports", response_model=Page[AdminEventReportResponse])
async def list_event_reports(
    status: EventReportStatusValue | None = None,
    reason: EventReportReasonValue | None = None,
    event_id: UUID | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> Page[AdminEventReportResponse]:
    return await report_service.list_reports(
        db, status=status.value if status else None, reason=reason.value if reason else None,
        event_id=event_id, limit=limit, offset=offset,
    )


@router.get("/reports/{report_id}", response_model=AdminEventReportResponse)
async def get_event_report(
    report_id: UUID, db: AsyncSession = Depends(get_db)
) -> AdminEventReportResponse:
    return await report_service.get_report(db, report_id)


@router.patch("/reports/{report_id}", response_model=AdminEventReportResponse)
async def update_event_report(
    report_id: UUID,
    payload: EventReportUpdate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminEventReportResponse:
    """Review a report. ``suspend_event`` (with RESOLVED) also suspends the event,
    using ``resolution_note`` as the suspension reason."""
    return await report_service.update_report(
        db, admin, report_id, status=payload.status.value, note=payload.resolution_note,
        suspend_event=payload.suspend_event,
    )


# ---------- Platform settings (SRS 4.12) ----------


def _settings_response(row: PlatformSettings) -> PlatformSettingsResponse:
    return PlatformSettingsResponse(
        activation_fee_amount=row.activation_fee_amount,
        activation_fee_currency=row.activation_fee_currency,
        processing_fee_percent=row.processing_fee_percent,
        updated_at=row.updated_at,
        updated_by_user_id=row.updated_by_user_id,
    )


@router.get("/settings", response_model=PlatformSettingsResponse)
async def get_platform_settings(db: AsyncSession = Depends(get_db)) -> PlatformSettingsResponse:
    return _settings_response(await platform_settings.get_settings_row(db))


@router.patch("/settings", response_model=PlatformSettingsResponse)
async def update_platform_settings(
    payload: PlatformSettingsUpdate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> PlatformSettingsResponse:
    """Change the activation fee and/or processing fee. Only new charges use the
    new values: paid activation fees and existing orders keep theirs."""
    row = await platform_settings.update_settings(
        db, admin, activation_fee_amount=payload.activation_fee_amount,
        processing_fee_percent=payload.processing_fee_percent, reason=payload.reason,
    )
    return _settings_response(row)
