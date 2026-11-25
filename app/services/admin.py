"""Platform administration (SCRUM-17): search, suspension and monitoring.

Every administrative action is recorded in the existing append-only
``audit_logs`` (actor_type PLATFORM_ADMIN, action ``admin.*``) with the
target entity, acting admin, reason (metadata), timestamp and the previous
and resulting state (before/after). The current suspension state is also kept
on the target row so enforcement is a column check.

Suspension never edits historical financial data: existing orders, tickets,
payments and refunds keep following their normal lifecycle.
"""

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Integer, String, func, literal, or_, select, union_all
from sqlalchemy import cast as sql_cast
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.errors import ApiError
from app.db import repositories as repo
from app.db.models import (
    AuditLog,
    Campaign,
    DisputeStatus,
    Event,
    EventSalesActivation,
    EventStatus,
    Order,
    OrderNotification,
    OrderStatus,
    OrganizerProfile,
    Payment,
    PaymentDispute,
    PaymentStatus,
    PayoutAccount,
    PayoutStatus,
    Refund,
    RefundStatus,
    SupportCase,
    User,
    UserRole,
    UserStatus,
    VerificationStatus,
)
from app.schemas.admin import (
    ActivationChecklist,
    AdminActionResponse,
    AdminActivationResponse,
    AdminCampaignResponse,
    AdminEventResponse,
    AdminOrderResponse,
    AdminPaymentResponse,
    AdminPayoutAccountResponse,
    AdminRefundResponse,
    AdminUserResponse,
    DisputeResponse,
    EventRef,
    OperationalFailureResponse,
    Page,
    PersonRef,
)
from app.services import notifications
from app.services.analytics import validate_range
from app.services.campaigns import effective_status
from app.services.organizers import mask_external_account_ref

logger = logging.getLogger("app.admin")

ADMIN_ACTOR = "PLATFORM_ADMIN"
_CAPTURED = (PaymentStatus.SUCCEEDED, PaymentStatus.REFUNDED, PaymentStatus.PARTIALLY_REFUNDED)
_DISPUTE_TRANSITIONS = {
    DisputeStatus.OPEN.value: {"UNDER_REVIEW", "RESOLVED", "REJECTED"},
    DisputeStatus.UNDER_REVIEW.value: {"RESOLVED", "REJECTED"},
    DisputeStatus.RESOLVED.value: set(),
    DisputeStatus.REJECTED.value: set(),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _pattern(q: str) -> str:
    return f"%{repo._escape_like(q.strip())}%"


def _as_uuid(value: str) -> UUID | None:
    try:
        return uuid.UUID(value.strip())
    except ValueError:
        return None


def _order_by(columns: dict[str, Any], sort: str, tiebreak: Any) -> tuple[Any, Any]:
    column = columns[sort.lstrip("-")]
    if sort.startswith("-"):
        return column.desc(), tiebreak.desc()
    return column.asc(), tiebreak.asc()


async def _page(
    db: AsyncSession, stmt: Any, count_stmt: Any, *, order: tuple[Any, Any], limit: int, offset: int
) -> tuple[list[Any], int]:
    total = int(await db.scalar(count_stmt) or 0)
    rows = (await db.execute(stmt.order_by(*order).limit(limit).offset(offset))).all()
    return list(rows), total


async def _audit(
    db: AsyncSession,
    admin: User,
    *,
    action: str,
    entity_type: str,
    entity_id: UUID,
    reason: str | None,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    event_id: UUID | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    await repo.add_audit_entry(
        db,
        actor_user_id=admin.id,
        actor_type=ADMIN_ACTOR,
        event_id=event_id,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        before_data=before,
        after_data=after,
        metadata={"reason": reason, **(extra or {})},
    )


# ---------- Users ----------

def _user_response(user: User, profile: OrganizerProfile | None) -> AdminUserResponse:
    return AdminUserResponse(
        id=user.id,
        user_id=user.id,
        name=f"{user.first_name} {user.last_name}".strip(),
        email=user.email,
        first_name=user.first_name,
        last_name=user.last_name,
        phone=user.phone,
        global_role=user.global_role.value,
        status=user.status.value,
        email_verified_at=user.email_verified_at,
        is_organizer=profile is not None,
        organizer_display_name=profile.display_name if profile else None,
        organizer_verification_status=profile.verification_status.value if profile else None,
        suspended_at=user.suspended_at,
        suspension_reason=user.suspension_reason,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


async def search_users(
    db: AsyncSession,
    *,
    q: str | None,
    status: UserStatus | None,
    role: UserRole | None,
    is_organizer: bool | None,
    sort: str,
    limit: int,
    offset: int,
) -> Page[AdminUserResponse]:
    conditions: list[Any] = []
    if q:
        exact = _as_uuid(q)
        pattern = _pattern(q)
        clauses: list[Any] = [
            User.email.ilike(pattern, escape="\\"),
            User.first_name.ilike(pattern, escape="\\"),
            User.last_name.ilike(pattern, escape="\\"),
        ]
        if exact is not None:
            clauses.append(User.id == exact)
        conditions.append(or_(*clauses))
    if status is not None:
        conditions.append(User.status == status)
    if role is not None:
        conditions.append(User.global_role == role)
    if is_organizer is True:
        conditions.append(OrganizerProfile.user_id.is_not(None))
    elif is_organizer is False:
        conditions.append(OrganizerProfile.user_id.is_(None))
    base = select(User, OrganizerProfile).outerjoin(
        OrganizerProfile, OrganizerProfile.user_id == User.id
    ).where(*conditions)
    count = (
        select(func.count(User.id))
        .outerjoin(OrganizerProfile, OrganizerProfile.user_id == User.id)
        .where(*conditions)
    )
    rows, total = await _page(
        db, base, count,
        order=_order_by(
            {"created_at": User.created_at, "email": User.email, "last_name": User.last_name},
            sort, User.id,
        ),
        limit=limit, offset=offset,
    )
    return Page[AdminUserResponse](
        items=[_user_response(u, p) for u, p in rows], total=total, limit=limit, offset=offset
    )


async def get_user(db: AsyncSession, user_id: UUID) -> AdminUserResponse:
    user = await repo.get_user_by_id(db, user_id)
    if user is None:
        raise ApiError(404, "USER_NOT_FOUND", "User not found")
    return _user_response(user, await repo.get_organizer_profile(db, user.id))


async def suspend_user(
    db: AsyncSession, admin: User, user_id: UUID, reason: str
) -> AdminUserResponse:
    target = await db.scalar(select(User).where(User.id == user_id).with_for_update())
    if target is None or target.status == UserStatus.DELETED:
        raise ApiError(404, "USER_NOT_FOUND", "User not found")
    if target.id == admin.id:
        raise ApiError(409, "CANNOT_SUSPEND_SELF", "Admins cannot suspend their own account")
    if target.global_role == UserRole.PLATFORM_ADMIN:
        raise ApiError(409, "CANNOT_SUSPEND_ADMIN", "Platform Admin accounts cannot be suspended")
    if target.status == UserStatus.BLOCKED:
        raise ApiError(409, "USER_ALREADY_SUSPENDED", "User is already suspended")
    previous = target.status
    now = _now()
    target.status = UserStatus.BLOCKED
    target.suspended_at = now
    target.suspended_by_user_id = admin.id
    target.suspension_reason = reason
    target.updated_at = now
    # Refresh tokens stop working immediately; already-issued access tokens
    # are rejected by get_current_user, which re-reads the status per request.
    revoked = await repo.revoke_all_user_sessions(db, target.id, now)
    await _audit(
        db, admin, action="admin.user_suspended", entity_type="user", entity_id=target.id,
        reason=reason, before={"status": previous.value},
        after={"status": target.status.value}, extra={"revoked_sessions": revoked},
    )
    logger.info("user %s suspended by admin %s", target.id, admin.id)
    return _user_response(target, await repo.get_organizer_profile(db, target.id))


async def unsuspend_user(
    db: AsyncSession, admin: User, user_id: UUID, reason: str
) -> AdminUserResponse:
    target = await db.scalar(select(User).where(User.id == user_id).with_for_update())
    if target is None or target.status == UserStatus.DELETED:
        raise ApiError(404, "USER_NOT_FOUND", "User not found")
    if target.status != UserStatus.BLOCKED:
        raise ApiError(409, "USER_NOT_SUSPENDED", "User is not suspended")
    now = _now()
    before = {
        "status": target.status.value,
        "suspended_at": target.suspended_at.isoformat() if target.suspended_at else None,
        "suspension_reason": target.suspension_reason,
    }
    target.status = UserStatus.ACTIVE
    target.suspended_at = None
    target.suspended_by_user_id = None
    target.suspension_reason = None
    target.updated_at = now
    await _audit(
        db, admin, action="admin.user_unsuspended", entity_type="user", entity_id=target.id,
        reason=reason, before=before, after={"status": target.status.value},
    )
    return _user_response(target, await repo.get_organizer_profile(db, target.id))


# ---------- Events ----------

def _event_query() -> Any:
    owner = aliased(User)
    return (
        select(
            Event, owner.email, OrganizerProfile.display_name, EventSalesActivation,
            owner.first_name, owner.last_name,
        )
        .outerjoin(owner, owner.id == Event.owner_user_id)
        .outerjoin(OrganizerProfile, OrganizerProfile.user_id == Event.owner_user_id)
        .outerjoin(EventSalesActivation, EventSalesActivation.event_id == Event.id)
    )


def _event_response(row: Any) -> AdminEventResponse:
    event, owner_email, display_name, activation, first, last = row
    owner_name = display_name or f"{first or ''} {last or ''}".strip()
    return AdminEventResponse(
        id=event.id,
        title=event.title,
        owner=PersonRef(id=event.owner_user_id, name=owner_name),
        owner_user_id=event.owner_user_id,
        owner_email=owner_email,
        organizer_display_name=display_name,
        status=event.status.value,
        visibility=event.visibility.value,
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        city=event.city,
        capacity=event.capacity,
        is_suspended=event.suspended_at is not None,
        suspended_at=event.suspended_at,
        suspension_reason=event.suspension_reason,
        paid_sales_activated=activation is not None and activation.activated_at is not None,
        paid_sales_suspended=activation is not None and activation.sales_suspended_at is not None,
        published_at=event.published_at,
        cancelled_at=event.cancelled_at,
        created_at=event.created_at,
    )


async def search_events(
    db: AsyncSession,
    *,
    q: str | None,
    status: EventStatus | None,
    suspended: bool | None,
    owner_user_id: UUID | None,
    starts_from: datetime | None,
    starts_to: datetime | None,
    sort: str,
    limit: int,
    offset: int,
) -> Page[AdminEventResponse]:
    starts_from, starts_to = validate_range(starts_from, starts_to)
    conditions: list[Any] = []
    if q:
        exact = _as_uuid(q)
        pattern = _pattern(q)
        clauses: list[Any] = [
            Event.title.ilike(pattern, escape="\\"),
            Event.city.ilike(pattern, escape="\\"),
            Event.venue_name.ilike(pattern, escape="\\"),
        ]
        if exact is not None:
            clauses.append(Event.id == exact)
        conditions.append(or_(*clauses))
    if status is not None:
        conditions.append(Event.status == status)
    if suspended is True:
        conditions.append(Event.suspended_at.is_not(None))
    elif suspended is False:
        conditions.append(Event.suspended_at.is_(None))
    if owner_user_id is not None:
        conditions.append(Event.owner_user_id == owner_user_id)
    if starts_from is not None:
        conditions.append(Event.starts_at >= starts_from)
    if starts_to is not None:
        conditions.append(Event.starts_at < starts_to)
    rows, total = await _page(
        db, _event_query().where(*conditions),
        select(func.count(Event.id)).where(*conditions),
        order=_order_by(
            {"created_at": Event.created_at, "starts_at": Event.starts_at, "title": Event.title},
            sort, Event.id,
        ),
        limit=limit, offset=offset,
    )
    return Page[AdminEventResponse](
        items=[_event_response(r) for r in rows], total=total, limit=limit, offset=offset
    )


async def get_event(db: AsyncSession, event_id: UUID) -> AdminEventResponse:
    row = (await db.execute(_event_query().where(Event.id == event_id))).first()
    if row is None:
        raise ApiError(404, "EVENT_NOT_FOUND", "Event not found")
    return _event_response(row)


async def _lock_event(db: AsyncSession, event_id: UUID) -> Event:
    # Same lock checkout takes first, so suspension and order creation
    # serialize: no order can be created after the suspension commits.
    event = await db.scalar(
        select(Event).where(Event.id == event_id).with_for_update()
        .execution_options(populate_existing=True)
    )
    if event is None:
        raise ApiError(404, "EVENT_NOT_FOUND", "Event not found")
    return event


async def _order_counts(db: AsyncSession, event_id: UUID) -> tuple[int, int]:
    rows = (
        await db.execute(
            select(Order.status, func.count(Order.id))
            .where(Order.event_id == event_id)
            .group_by(Order.status)
        )
    ).all()
    counts = {status: int(count) for status, count in rows}
    pending = counts.get(OrderStatus.PENDING, 0)
    confirmed = counts.get(OrderStatus.PAID, 0) + counts.get(OrderStatus.CONFIRMED, 0)
    return pending, confirmed


async def _event_action_response(db: AsyncSession, event: Event) -> dict[str, Any]:
    await db.flush()
    pending, confirmed = await _order_counts(db, event.id)
    return {
        "event": await get_event(db, event.id),
        "pending_orders": pending,
        "confirmed_orders": confirmed,
    }


async def suspend_event(
    db: AsyncSession, admin: User, event_id: UUID, reason: str
) -> dict[str, Any]:
    event = await _lock_event(db, event_id)
    if event.status in (EventStatus.CANCELLED, EventStatus.COMPLETED):
        raise ApiError(
            409, "EVENT_NOT_SUSPENDABLE", f"A {event.status.value} event cannot be suspended"
        )
    if event.suspended_at is not None:
        raise ApiError(409, "EVENT_ALREADY_SUSPENDED", "Event is already suspended")
    now = _now()
    event.suspended_at = now
    event.suspended_by_user_id = admin.id
    event.suspension_reason = reason
    event.updated_at = now
    await _audit(
        db, admin, action="admin.event_suspended", entity_type="event", entity_id=event.id,
        event_id=event.id, reason=reason,
        before={"suspended": False, "status": event.status.value},
        after={"suspended": True, "status": event.status.value},
    )
    await notifications.notify_users(
        db, user_ids=[event.owner_user_id], kind="EVENT_SUSPENDED",
        payload={"event_id": str(event.id), "title": event.title, "reason": reason},
        exclude=[admin.id],
    )
    logger.info("event %s suspended by admin %s", event.id, admin.id)
    return await _event_action_response(db, event)


async def unsuspend_event(
    db: AsyncSession, admin: User, event_id: UUID, reason: str
) -> dict[str, Any]:
    event = await _lock_event(db, event_id)
    if event.suspended_at is None:
        raise ApiError(409, "EVENT_NOT_SUSPENDED", "Event is not suspended")
    before = {
        "suspended": True,
        "status": event.status.value,
        "suspended_at": event.suspended_at.isoformat(),
        "suspension_reason": event.suspension_reason,
    }
    event.suspended_at = None
    event.suspended_by_user_id = None
    event.suspension_reason = None
    event.updated_at = _now()
    await _audit(
        db, admin, action="admin.event_unsuspended", entity_type="event", entity_id=event.id,
        event_id=event.id, reason=reason, before=before,
        after={"suspended": False, "status": event.status.value},
    )
    await notifications.notify_users(
        db, user_ids=[event.owner_user_id], kind="EVENT_UNSUSPENDED",
        payload={"event_id": str(event.id), "title": event.title},
        exclude=[admin.id],
    )
    return await _event_action_response(db, event)


async def stop_paid_sales(
    db: AsyncSession, admin: User, event_id: UUID, reason: str
) -> dict[str, Any]:
    from app.services.paid_sales import get_or_create_activation

    event = await _lock_event(db, event_id)
    activation = await get_or_create_activation(db, event)
    if activation.sales_suspended_at is not None:
        raise ApiError(409, "PAID_SALES_ALREADY_STOPPED", "Paid sales are already stopped")
    now = _now()
    activation.sales_suspended_at = now
    activation.sales_suspended_by_user_id = admin.id
    activation.sales_suspension_reason = reason
    activation.updated_at = now
    await _audit(
        db, admin, action="admin.paid_sales_stopped", entity_type="event_sales_activation",
        entity_id=event.id, event_id=event.id, reason=reason,
        before={"paid_sales_suspended": False, "activated": activation.activated_at is not None},
        after={"paid_sales_suspended": True, "activated": activation.activated_at is not None},
    )
    await notifications.notify_users(
        db, user_ids=[event.owner_user_id], kind="PAID_SALES_STOPPED",
        payload={"event_id": str(event.id), "title": event.title, "reason": reason},
        exclude=[admin.id],
    )
    return await _event_action_response(db, event)


async def resume_paid_sales(
    db: AsyncSession, admin: User, event_id: UUID, reason: str
) -> dict[str, Any]:
    event = await _lock_event(db, event_id)
    activation = await db.get(EventSalesActivation, event.id)
    if activation is None or activation.sales_suspended_at is None:
        raise ApiError(409, "PAID_SALES_NOT_STOPPED", "Paid sales are not stopped")
    before = {
        "paid_sales_suspended": True,
        "suspended_at": activation.sales_suspended_at.isoformat(),
        "suspension_reason": activation.sales_suspension_reason,
    }
    activation.sales_suspended_at = None
    activation.sales_suspended_by_user_id = None
    activation.sales_suspension_reason = None
    activation.updated_at = _now()
    await _audit(
        db, admin, action="admin.paid_sales_resumed", entity_type="event_sales_activation",
        entity_id=event.id, event_id=event.id, reason=reason, before=before,
        after={"paid_sales_suspended": False},
    )
    return await _event_action_response(db, event)


# ---------- Orders and payments ----------

async def search_orders(
    db: AsyncSession,
    *,
    q: str | None,
    status: OrderStatus | None,
    event_id: UUID | None,
    buyer_user_id: UUID | None,
    created_from: datetime | None,
    created_to: datetime | None,
    sort: str,
    limit: int,
    offset: int,
) -> Page[AdminOrderResponse]:
    created_from, created_to = validate_range(created_from, created_to)
    conditions: list[Any] = []
    if q:
        clauses: list[Any] = [Order.buyer_email.ilike(_pattern(q), escape="\\")]
        if q.strip().isdigit():
            clauses.append(Order.order_number == int(q.strip()))
        exact = _as_uuid(q)
        if exact is not None:
            clauses.append(Order.id == exact)
        conditions.append(or_(*clauses))
    if status is not None:
        conditions.append(Order.status == status)
    if event_id is not None:
        conditions.append(Order.event_id == event_id)
    if buyer_user_id is not None:
        conditions.append(Order.buyer_user_id == buyer_user_id)
    if created_from is not None:
        conditions.append(Order.created_at >= created_from)
    if created_to is not None:
        conditions.append(Order.created_at < created_to)
    rows, total = await _page(
        db,
        select(Order, Event.title).outerjoin(Event, Event.id == Order.event_id).where(*conditions),
        select(func.count(Order.id)).where(*conditions),
        order=_order_by(
            {"created_at": Order.created_at, "total_amount": Order.total_amount,
             "paid_at": Order.paid_at},
            sort, Order.id,
        ),
        limit=limit, offset=offset,
    )
    items = [
        AdminOrderResponse(
            id=o.id, order_number=o.order_number,
            event=EventRef(id=o.event_id, title=title) if title is not None else None,
            event_id=o.event_id, event_title=title,
            buyer_user_id=o.buyer_user_id, buyer_email=o.buyer_email, status=o.status.value,
            currency=o.currency, subtotal_amount=o.subtotal_amount,
            discount_amount=o.discount_amount, fee_amount=o.fee_amount,
            total_amount=o.total_amount, campaign_id=o.campaign_id,
            promo_code=o.promo_code_snapshot, expires_at=o.expires_at, paid_at=o.paid_at,
            cancelled_at=o.cancelled_at, created_at=o.created_at,
        )
        for o, title in rows
    ]
    return Page[AdminOrderResponse](items=items, total=total, limit=limit, offset=offset)


def _payment_response(
    payment: Payment, order_number: int | None, event_id: UUID | None
) -> AdminPaymentResponse:
    return AdminPaymentResponse(
        id=payment.id, order_id=payment.order_id, order_number=order_number, event_id=event_id,
        provider=payment.provider, provider_payment_ref=payment.provider_payment_ref,
        amount=payment.amount, currency=payment.currency, status=payment.status.value,
        is_simulated=payment.is_simulated, failure_code=payment.failure_code,
        failure_message=payment.failure_message, paid_at=payment.paid_at,
        created_at=payment.created_at, updated_at=payment.updated_at,
    )


async def search_payments(
    db: AsyncSession,
    *,
    q: str | None,
    status: PaymentStatus | None,
    provider: str | None,
    order_id: UUID | None,
    event_id: UUID | None,
    created_from: datetime | None,
    created_to: datetime | None,
    sort: str,
    limit: int,
    offset: int,
) -> Page[AdminPaymentResponse]:
    created_from, created_to = validate_range(created_from, created_to)
    conditions: list[Any] = []
    if q:
        clauses: list[Any] = [Payment.provider_payment_ref.ilike(_pattern(q), escape="\\")]
        if q.strip().isdigit():
            clauses.append(Order.order_number == int(q.strip()))
        exact = _as_uuid(q)
        if exact is not None:
            clauses.extend([Payment.id == exact, Payment.order_id == exact])
        conditions.append(or_(*clauses))
    if status is not None:
        conditions.append(Payment.status == status)
    if provider:
        conditions.append(Payment.provider == provider)
    if order_id is not None:
        conditions.append(Payment.order_id == order_id)
    if event_id is not None:
        conditions.append(Order.event_id == event_id)
    if created_from is not None:
        conditions.append(Payment.created_at >= created_from)
    if created_to is not None:
        conditions.append(Payment.created_at < created_to)
    rows, total = await _page(
        db,
        select(Payment, Order.order_number, Order.event_id)
        .join(Order, Order.id == Payment.order_id).where(*conditions),
        select(func.count(Payment.id)).join(Order, Order.id == Payment.order_id).where(*conditions),
        order=_order_by(
            {"created_at": Payment.created_at, "amount": Payment.amount}, sort, Payment.id
        ),
        limit=limit, offset=offset,
    )
    return Page[AdminPaymentResponse](
        items=[_payment_response(p, number, ev) for p, number, ev in rows],
        total=total, limit=limit, offset=offset,
    )


# ---------- Monitoring ----------

def _activation_status(activation: EventSalesActivation) -> str:
    if activation.sales_suspended_at is not None:
        return "SUSPENDED"
    if activation.activated_at is not None:
        return "ACTIVE"
    return "NOT_ACTIVATED"


async def list_activations(
    db: AsyncSession, *, status: str | None, q: str | None, limit: int, offset: int
) -> Page[AdminActivationResponse]:
    conditions: list[Any] = []
    if q:
        conditions.append(or_(
            Event.title.ilike(_pattern(q), escape="\\"),
            OrganizerProfile.display_name.ilike(_pattern(q), escape="\\"),
        ))
    if status == "SUSPENDED":
        conditions.append(EventSalesActivation.sales_suspended_at.is_not(None))
    elif status == "ACTIVE":
        conditions += [
            EventSalesActivation.activated_at.is_not(None),
            EventSalesActivation.sales_suspended_at.is_(None),
        ]
    elif status == "NOT_ACTIVATED":
        conditions += [
            EventSalesActivation.activated_at.is_(None),
            EventSalesActivation.sales_suspended_at.is_(None),
        ]
    payout_ok = (
        select(PayoutAccount.id)
        .where(
            PayoutAccount.organizer_user_id == Event.owner_user_id,
            PayoutAccount.status == PayoutStatus.ACTIVE,
        )
        .correlate(Event)
        .exists()
    )
    rows, total = await _page(
        db,
        select(EventSalesActivation, Event.title, Event.owner_user_id,
               OrganizerProfile.display_name, OrganizerProfile.verification_status,
               payout_ok)
        .outerjoin(Event, Event.id == EventSalesActivation.event_id)
        .outerjoin(OrganizerProfile, OrganizerProfile.user_id == Event.owner_user_id)
        .where(*conditions),
        select(func.count(EventSalesActivation.event_id))
        .outerjoin(Event, Event.id == EventSalesActivation.event_id)
        .outerjoin(OrganizerProfile, OrganizerProfile.user_id == Event.owner_user_id)
        .where(*conditions),
        order=(EventSalesActivation.updated_at.desc(), EventSalesActivation.event_id.desc()),
        limit=limit, offset=offset,
    )
    items = [
        AdminActivationResponse(
            event=EventRef(id=a.event_id, title=title) if title is not None else None,
            organizer=(
                PersonRef(id=owner_id, name=display_name or "") if owner_id is not None else None
            ),
            checklist=ActivationChecklist(
                verification_complete=verification == VerificationStatus.VERIFIED,
                payout_account_complete=bool(has_payout),
                activation_fee_paid=a.activation_fee_paid_at is not None,
                terms_accepted=a.terms_accepted_at is not None,
            ),
            event_id=a.event_id, event_title=title, owner_user_id=owner_id,
            organizer_display_name=display_name,
            organizer_verification_status=verification.value if verification else None,
            status=_activation_status(a), activation_fee_amount=a.activation_fee_amount,
            activation_fee_paid_at=a.activation_fee_paid_at,
            terms_accepted_at=a.terms_accepted_at, activated_at=a.activated_at,
            sales_suspended_at=a.sales_suspended_at,
            sales_suspension_reason=a.sales_suspension_reason,
            created_at=a.created_at, updated_at=a.updated_at,
        )
        for a, title, owner_id, display_name, verification, has_payout in rows
    ]
    return Page[AdminActivationResponse](items=items, total=total, limit=limit, offset=offset)


async def list_payout_accounts(
    db: AsyncSession, *, status: PayoutStatus | None, q: str | None, limit: int, offset: int
) -> Page[AdminPayoutAccountResponse]:
    """Payout accounts for the approval queue; ``q`` matches organizer name or email."""
    conditions: list[Any] = []
    if status is not None:
        conditions.append(PayoutAccount.status == status)
    if q:
        conditions.append(or_(
            OrganizerProfile.display_name.ilike(_pattern(q), escape="\\"),
            User.email.ilike(_pattern(q), escape="\\"),
        ))
    rows, total = await _page(
        db,
        select(PayoutAccount, OrganizerProfile.display_name,
               OrganizerProfile.verification_status, User.email)
        .join(OrganizerProfile, OrganizerProfile.user_id == PayoutAccount.organizer_user_id)
        .outerjoin(User, User.id == PayoutAccount.organizer_user_id)
        .where(*conditions),
        select(func.count(PayoutAccount.id))
        .join(OrganizerProfile, OrganizerProfile.user_id == PayoutAccount.organizer_user_id)
        .outerjoin(User, User.id == PayoutAccount.organizer_user_id)
        .where(*conditions),
        order=(PayoutAccount.updated_at.desc(), PayoutAccount.id.desc()),
        limit=limit, offset=offset,
    )
    items = [
        AdminPayoutAccountResponse(
            id=a.id,
            organizer=PersonRef(id=a.organizer_user_id, name=display_name or ""),
            organizer_email=email,
            organizer_verification_status=verification.value,
            provider=a.provider,
            external_account_ref=mask_external_account_ref(a.external_account_ref),
            status=a.status.value, is_default=a.is_default,
            created_at=a.created_at, updated_at=a.updated_at,
        )
        for a, display_name, verification, email in rows
    ]
    return Page[AdminPayoutAccountResponse](items=items, total=total, limit=limit, offset=offset)


async def list_refunds(
    db: AsyncSession,
    *,
    status: RefundStatus | None,
    kind: str | None,
    event_id: UUID | None,
    created_from: datetime | None,
    created_to: datetime | None,
    limit: int,
    offset: int,
    q: str | None = None,
) -> Page[AdminRefundResponse]:
    created_from, created_to = validate_range(created_from, created_to)
    conditions: list[Any] = []
    if q:
        clauses: list[Any] = [
            Event.title.ilike(_pattern(q), escape="\\"),
            Order.buyer_email.ilike(_pattern(q), escape="\\"),
        ]
        if q.strip().isdigit():
            clauses.append(Order.order_number == int(q.strip()))
        conditions.append(or_(*clauses))
    if status is not None:
        conditions.append(Refund.status == status)
    if kind:
        conditions.append(Refund.kind == kind)
    if event_id is not None:
        conditions.append(Order.event_id == event_id)
    if created_from is not None:
        conditions.append(Refund.created_at >= created_from)
    if created_to is not None:
        conditions.append(Refund.created_at < created_to)
    rows, total = await _page(
        db,
        select(Refund, Order.order_number, Order.event_id, Event.title)
        .join(Order, Order.id == Refund.order_id)
        .outerjoin(Event, Event.id == Order.event_id).where(*conditions),
        select(func.count(Refund.id)).join(Order, Order.id == Refund.order_id)
        .outerjoin(Event, Event.id == Order.event_id).where(*conditions),
        order=(Refund.created_at.desc(), Refund.id.desc()),
        limit=limit, offset=offset,
    )
    items = [
        AdminRefundResponse(
            id=r.id, order_id=r.order_id, order_number=number,
            event=EventRef(id=ev, title=title) if title is not None else None, event_id=ev,
            payment_id=r.payment_id, amount=r.amount, currency=r.currency,
            status=r.status.value, kind=r.kind, reason=r.reason, is_simulated=r.is_simulated,
            initiated_by=r.initiated_by, created_at=r.created_at, completed_at=r.completed_at,
        )
        for r, number, ev, title in rows
    ]
    return Page[AdminRefundResponse](items=items, total=total, limit=limit, offset=offset)


async def list_failures(
    db: AsyncSession,
    *,
    kind: str | None,
    created_from: datetime | None,
    created_to: datetime | None,
    limit: int,
    offset: int,
) -> Page[OperationalFailureResponse]:
    """Failed payments, failed refunds and failed outbox emails, newest first.

    All three come from their own authoritative tables (UNION ALL); nothing is
    synthesized. Notification errors are already secret-free by design.
    """
    created_from, created_to = validate_range(created_from, created_to)

    def ranged(column: Any) -> list[Any]:
        out = []
        if created_from is not None:
            out.append(column >= created_from)
        if created_to is not None:
            out.append(column < created_to)
        return out

    null_int = sql_cast(literal(None), Integer).label("attempts")
    selects: dict[str, Any] = {
        "PAYMENT": select(
            literal("PAYMENT").label("kind"), Payment.id.label("id"),
            Payment.order_id.label("order_id"), Order.event_id.label("event_id"),
            Payment.failure_code.label("code"), Payment.failure_message.label("message"),
            null_int, Payment.updated_at.label("occurred_at"),
        ).join(Order, Order.id == Payment.order_id).where(
            Payment.status == PaymentStatus.FAILED, *ranged(Payment.updated_at)
        ),
        "REFUND": select(
            literal("REFUND").label("kind"), Refund.id.label("id"),
            Refund.order_id.label("order_id"), Order.event_id.label("event_id"),
            sql_cast(literal("REFUND_FAILED"), String).label("code"),
            Refund.reason.label("message"), null_int, Refund.updated_at.label("occurred_at"),
        ).join(Order, Order.id == Refund.order_id).where(
            Refund.status == RefundStatus.FAILED, *ranged(Refund.updated_at)
        ),
        "NOTIFICATION": select(
            literal("NOTIFICATION").label("kind"), OrderNotification.id.label("id"),
            OrderNotification.order_id.label("order_id"), Order.event_id.label("event_id"),
            OrderNotification.kind.label("code"), OrderNotification.last_error.label("message"),
            sql_cast(OrderNotification.attempts, Integer).label("attempts"),
            OrderNotification.updated_at.label("occurred_at"),
        ).join(Order, Order.id == OrderNotification.order_id).where(
            OrderNotification.status == "FAILED", *ranged(OrderNotification.updated_at)
        ),
    }
    chosen = [selects[kind]] if kind else list(selects.values())
    union = union_all(*chosen).subquery()
    total = int(await db.scalar(select(func.count()).select_from(union)) or 0)
    rows = (
        await db.execute(
            select(union)
            .order_by(union.c.occurred_at.desc(), union.c.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    items = [
        OperationalFailureResponse(
            kind=row.kind,
            id=row.id if isinstance(row.id, UUID) else UUID(str(row.id)),
            order_id=(
                row.order_id if row.order_id is None or isinstance(row.order_id, UUID)
                else UUID(str(row.order_id))
            ),
            event_id=(
                row.event_id if row.event_id is None or isinstance(row.event_id, UUID)
                else UUID(str(row.event_id))
            ),
            code=row.code,
            message=row.message,
            attempts=int(row.attempts) if row.attempts is not None else None,
            occurred_at=_aware(row.occurred_at),
        )
        for row in rows
    ]
    return Page[OperationalFailureResponse](items=items, total=total, limit=limit, offset=offset)


def _aware(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


async def list_admin_actions(
    db: AsyncSession,
    *,
    action: str | None,
    entity_type: str | None,
    entity_id: UUID | None,
    limit: int,
    offset: int,
) -> Page[AdminActionResponse]:
    conditions: list[Any] = [
        AuditLog.actor_type == ADMIN_ACTOR,
        AuditLog.action.startswith("admin."),
    ]
    if action:
        conditions.append(AuditLog.action == action)
    if entity_type:
        conditions.append(AuditLog.entity_type == entity_type)
    if entity_id is not None:
        conditions.append(AuditLog.entity_id == entity_id)
    actor = aliased(User)
    rows, total = await _page(
        db,
        select(AuditLog, actor).outerjoin(actor, actor.id == AuditLog.actor_user_id)
        .where(*conditions),
        select(func.count()).select_from(AuditLog).where(*conditions),
        order=(AuditLog.created_at.desc(), AuditLog.id.desc()),
        limit=limit, offset=offset,
    )
    items = [
        AdminActionResponse(
            id=entry.id, timestamp=entry.created_at, admin_user_id=entry.actor_user_id,
            admin_display_name=f"{a.first_name} {a.last_name}".strip() if a else None,
            action=entry.action, entity_type=entry.entity_type, entity_id=entry.entity_id,
            event_id=entry.event_id, reason=(entry.meta or {}).get("reason"),
            before=entry.before_data, after=entry.after_data,
        )
        for entry, a in rows
    ]
    return Page[AdminActionResponse](items=items, total=total, limit=limit, offset=offset)


# ---------- Disputes ----------

def _dispute_response(d: PaymentDispute) -> DisputeResponse:
    return DisputeResponse(
        id=d.id, payment_id=d.payment_id, order_id=d.order_id, event_id=d.event_id,
        support_case_id=d.support_case_id, status=d.status, reason=d.reason, amount=d.amount,
        currency=d.currency, resolution_note=d.resolution_note,
        opened_by_user_id=d.opened_by_user_id, resolved_by_user_id=d.resolved_by_user_id,
        resolved_at=d.resolved_at, created_at=d.created_at, updated_at=d.updated_at,
    )


async def open_dispute(
    db: AsyncSession,
    admin: User,
    *,
    payment_id: UUID,
    reason: str,
    amount: Decimal | None,
    support_case_id: UUID | None,
) -> DisputeResponse:
    payment = await db.get(Payment, payment_id)
    if payment is None:
        raise ApiError(404, "PAYMENT_NOT_FOUND", "Payment not found")
    if payment.status not in _CAPTURED:
        raise ApiError(
            409, "PAYMENT_NOT_DISPUTABLE", "Only captured payments can be disputed"
        )
    order = await repo.get_order_by_id(db, payment.order_id)
    assert order is not None
    disputed = amount if amount is not None else payment.amount
    if disputed > payment.amount:
        raise ApiError(422, "DISPUTE_AMOUNT_INVALID", "Disputed amount exceeds the payment")
    if support_case_id is not None:
        case = await db.get(SupportCase, support_case_id)
        if case is None or (case.order_id is not None and case.order_id != order.id):
            raise ApiError(
                422, "DISPUTE_CASE_MISMATCH", "Support case does not belong to this payment"
            )
    now = _now()
    dispute = PaymentDispute(
        payment_id=payment.id, order_id=order.id, event_id=order.event_id,
        support_case_id=support_case_id, opened_by_user_id=admin.id,
        status=DisputeStatus.OPEN.value, reason=reason.strip(), amount=disputed,
        currency=payment.currency, created_at=now, updated_at=now,
    )
    db.add(dispute)
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as exc:
        raise ApiError(
            409, "DISPUTE_ALREADY_OPEN", "This payment already has an open dispute"
        ) from exc
    await _audit(
        db, admin, action="admin.dispute_opened", entity_type="payment_dispute",
        entity_id=dispute.id, event_id=order.event_id, reason=dispute.reason,
        before=None, after={"status": dispute.status},
        extra={"payment_id": str(payment.id), "amount": str(disputed)},
    )
    return _dispute_response(dispute)


async def list_disputes(
    db: AsyncSession, *, status: str | None, event_id: UUID | None, limit: int, offset: int
) -> Page[DisputeResponse]:
    conditions: list[Any] = []
    if status:
        conditions.append(PaymentDispute.status == status)
    if event_id is not None:
        conditions.append(PaymentDispute.event_id == event_id)
    rows, total = await _page(
        db, select(PaymentDispute).where(*conditions),
        select(func.count(PaymentDispute.id)).where(*conditions),
        order=(PaymentDispute.created_at.desc(), PaymentDispute.id.desc()),
        limit=limit, offset=offset,
    )
    return Page[DisputeResponse](
        items=[_dispute_response(r[0]) for r in rows], total=total, limit=limit, offset=offset
    )


async def update_dispute(
    db: AsyncSession, admin: User, dispute_id: UUID, *, status: str, note: str | None
) -> DisputeResponse:
    dispute = await db.scalar(
        select(PaymentDispute).where(PaymentDispute.id == dispute_id).with_for_update()
    )
    if dispute is None:
        raise ApiError(404, "DISPUTE_NOT_FOUND", "Dispute not found")
    if status == dispute.status:
        return _dispute_response(dispute)
    if status not in _DISPUTE_TRANSITIONS[dispute.status]:
        raise ApiError(
            409, "INVALID_STATUS_TRANSITION",
            f"Cannot move a dispute from {dispute.status} to {status}",
        )
    previous = dispute.status
    now = _now()
    dispute.status = status
    dispute.resolution_note = note if note is not None else dispute.resolution_note
    dispute.updated_at = now
    if status in (DisputeStatus.RESOLVED.value, DisputeStatus.REJECTED.value):
        dispute.resolved_at = now
        dispute.resolved_by_user_id = admin.id
    await _audit(
        db, admin, action="admin.dispute_updated", entity_type="payment_dispute",
        entity_id=dispute.id, event_id=dispute.event_id, reason=note,
        before={"status": previous}, after={"status": status},
    )
    await db.flush()
    return _dispute_response(dispute)


# ---------- Campaigns ----------

async def list_campaigns(
    db: AsyncSession, *, q: str | None, event_id: UUID | None, limit: int, offset: int
) -> Page[AdminCampaignResponse]:
    conditions: list[Any] = []
    if q:
        conditions.append(or_(
            Campaign.name.ilike(_pattern(q), escape="\\"),
            Campaign.promo_code.ilike(_pattern(q), escape="\\"),
            Event.title.ilike(_pattern(q), escape="\\"),
        ))
    if event_id is not None:
        conditions.append(Campaign.event_id == event_id)
    rows, total = await _page(
        db,
        select(Campaign, Event.title).outerjoin(Event, Event.id == Campaign.event_id)
        .where(*conditions),
        select(func.count(Campaign.id)).outerjoin(Event, Event.id == Campaign.event_id)
        .where(*conditions),
        order=(Campaign.created_at.desc(), Campaign.id.desc()),
        limit=limit, offset=offset,
    )
    now = _now()
    items = [
        AdminCampaignResponse(
            id=c.id,
            event=EventRef(id=c.event_id, title=title) if title is not None else None,
            name=c.name, code=c.promo_code, status=c.status.value,
            effective_status=effective_status(c, now),
            discount_type=c.discount_type.value, discount_value=c.discount_value,
            redemption_count=c.redemption_count, redemption_limit=c.max_redemptions,
            starts_at=c.starts_at, ends_at=c.ends_at, created_at=c.created_at,
        )
        for c, title in rows
    ]
    return Page[AdminCampaignResponse](items=items, total=total, limit=limit, offset=offset)


# ---------- CSV export ----------

EXPORT_MAX_ROWS = 10_000
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(value: Any) -> str:
    """Stringify one cell; neutralize spreadsheet formula injection."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    text = value.value if hasattr(value, "value") and not isinstance(value, Decimal) else value
    text = str(text)
    if text.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + text
    return text


EXPORT_COLUMNS: dict[str, list[str]] = {
    "users": ["id", "email", "name", "global_role", "status", "is_organizer",
              "email_verified_at", "suspended_at", "created_at"],
    "events": ["id", "title", "owner_id", "owner_name", "status", "is_suspended",
               "starts_at", "ends_at", "city", "capacity", "created_at"],
    "orders": ["id", "order_number", "event_id", "event_title", "buyer_email", "status",
               "currency", "subtotal_amount", "discount_amount", "fee_amount", "total_amount",
               "promo_code", "paid_at", "created_at"],
    "payments": ["id", "order_id", "order_number", "event_id", "provider", "amount",
                 "currency", "status", "failure_code", "is_simulated", "paid_at",
                 "created_at"],
}


async def export_rows(db: AsyncSession, kind: str, *, q: str | None) -> list[list[str]]:
    """Rows for ``/admin/export/{kind}.csv`` (header first), newest first.

    Reuses the search functions in pages, so filters and redaction match the
    JSON listings (no password hashes, tokens or account references).
    """
    rows: list[list[str]] = [EXPORT_COLUMNS[kind]]
    page_size = 100
    offset = 0
    while offset < EXPORT_MAX_ROWS:
        values: list[list[Any]]
        if kind == "users":
            page_u = await search_users(db, q=q, status=None, role=None, is_organizer=None,
                                        sort="-created_at", limit=page_size, offset=offset)
            values = [[u.id, u.email, u.name, u.global_role, u.status, u.is_organizer,
                       u.email_verified_at, u.suspended_at, u.created_at] for u in page_u.items]
        elif kind == "events":
            page_e = await search_events(db, q=q, status=None, suspended=None,
                                         owner_user_id=None, starts_from=None, starts_to=None,
                                         sort="-created_at", limit=page_size, offset=offset)
            values = [[e.id, e.title, e.owner_user_id, e.owner.name if e.owner else None,
                       e.status, e.is_suspended, e.starts_at, e.ends_at, e.city, e.capacity,
                       e.created_at] for e in page_e.items]
        elif kind == "orders":
            page_o = await search_orders(db, q=q, status=None, event_id=None, buyer_user_id=None,
                                         created_from=None, created_to=None, sort="-created_at",
                                         limit=page_size, offset=offset)
            values = [[o.id, o.order_number, o.event_id, o.event_title, o.buyer_email, o.status,
                       o.currency, o.subtotal_amount, o.discount_amount, o.fee_amount,
                       o.total_amount, o.promo_code, o.paid_at, o.created_at]
                      for o in page_o.items]
        else:
            page_p = await search_payments(db, q=q, status=None, provider=None, order_id=None,
                                           event_id=None, created_from=None, created_to=None,
                                           sort="-created_at", limit=page_size, offset=offset)
            values = [[p.id, p.order_id, p.order_number, p.event_id, p.provider, p.amount,
                       p.currency, p.status, p.failure_code, p.is_simulated, p.paid_at,
                       p.created_at] for p in page_p.items]
        rows.extend([[_csv_cell(v) for v in row] for row in values])
        if len(values) < page_size:
            break
        offset += page_size
    return rows[: EXPORT_MAX_ROWS + 1]
