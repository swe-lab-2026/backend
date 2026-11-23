"""Cancellations, simulated refunds and event cancellation (SCRUM-14).

Lifecycle (statuses are the pre-existing enums):

    customer cancels FREE registration   Order CONFIRMED/PAID -> CANCELLED
                                          Tickets VALID -> CANCELLED
    customer refunds PAID order          Order PAID -> REFUNDED
                                          Payment SUCCEEDED -> REFUNDED
                                          Refund (new row) PENDING -> SUCCEEDED
                                          Tickets VALID -> REFUNDED
    pending checkout cancelled           Order PENDING -> CANCELLED,
                                          Payment PENDING -> CANCELLED, holds released

Every transition also releases the order's promo redemption (SCRUM-15) and
writes audit entries in the same transaction.

Two refund kinds are kept apart:

* CUSTOMER_REQUEST: subject to the event policy (``refunds_enabled`` and
  ``refund_deadline_hours`` before ``starts_at``); checked server-side.
* EVENT_CANCELLATION: organizer cancelled the event. Every paid order is fully
  refunded regardless of the customer policy; free registrations are cancelled
  without a monetary refund.

Idempotency and concurrency: the order row is locked (then its payment, the
same order the payment flow uses) before any state is read; a refund is only
created from PAID+SUCCEEDED, and ``uq_refunds_active_per_payment`` allows one
non-failed refund per payment. Repeating a request therefore returns the
existing result instead of paying out again.

Refunds are simulated (``is_simulated=True``): no payment provider is
contacted, so there is no network I/O while row locks are held. Emails are
queued in the outbox and sent after commit.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db import repositories as repo
from app.db.models import (
    Event,
    EventStatus,
    HoldStatus,
    InventoryHold,
    Order,
    OrderItem,
    OrderStatus,
    Payment,
    PaymentStatus,
    Refund,
    RefundStatus,
    Ticket,
    TicketStatus,
    User,
    UserRole,
)
from app.services import campaigns as campaign_service
from app.services import events as event_service
from app.services import notifications
from app.services.paid_sales import SIMULATION_PROVIDER

logger = logging.getLogger("app.refunds")

KIND_CUSTOMER = "CUSTOMER_REQUEST"
KIND_EVENT_CANCELLATION = "EVENT_CANCELLATION"
_COMMITTED = (OrderStatus.PAID, OrderStatus.CONFIRMED)
_ACTIVE = (OrderStatus.PENDING, *_COMMITTED)
_CLOSED = (OrderStatus.CANCELLED, OrderStatus.REFUNDED)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _actor_type(user: User, event: Event | None = None) -> str:
    if user.global_role == UserRole.PLATFORM_ADMIN:
        return "PLATFORM_ADMIN"
    if event is not None and event.owner_user_id == user.id:
        return "ORGANIZER"
    return "CUSTOMER"


# ---------- Policy ----------

def refund_deadline(event: Event) -> datetime:
    hours = event.refund_deadline_hours or 0
    return event.starts_at - timedelta(hours=hours)


def policy_view(event: Event) -> dict[str, Any]:
    return {
        "event_id": event.id,
        "refunds_enabled": event.refunds_enabled,
        "refund_deadline_hours": event.refund_deadline_hours,
        "refund_deadline": refund_deadline(event) if event.refunds_enabled else None,
        "policy_text": event.refund_policy,
    }


async def update_refund_policy(
    db: AsyncSession,
    actor: User,
    event: Event,
    *,
    refunds_enabled: bool,
    refund_deadline_hours: int | None,
    policy_text: str | None,
    fields: set[str],
) -> Event:
    if event.status in (EventStatus.CANCELLED, EventStatus.COMPLETED):
        raise ApiError(
            409, "EVENT_NOT_EDITABLE", "The refund policy of a closed event cannot change"
        )
    before = {
        "refunds_enabled": event.refunds_enabled,
        "refund_deadline_hours": event.refund_deadline_hours,
        "policy_text": event.refund_policy,
    }
    event.refunds_enabled = refunds_enabled
    event.refund_deadline_hours = refund_deadline_hours
    if "policy_text" in fields:
        event.refund_policy = policy_text
    event.updated_at = _now()
    after = {
        "refunds_enabled": event.refunds_enabled,
        "refund_deadline_hours": event.refund_deadline_hours,
        "policy_text": event.refund_policy,
    }
    if before != after:
        await repo.add_audit_entry(
            db,
            actor_user_id=actor.id,
            actor_type=_actor_type(actor, event),
            event_id=event.id,
            entity_type="event",
            entity_id=event.id,
            action="event.refund_policy_updated",
            before_data=before,
            after_data=after,
        )
    await db.flush()
    return event


def _policy_violation(event: Event, now: datetime) -> tuple[str, str] | None:
    if not event.refunds_enabled:
        return "REFUNDS_DISABLED", "Refunds are not available for this event"
    if now >= refund_deadline(event):
        return "REFUND_DEADLINE_PASSED", "The refund deadline for this event has passed"
    return None


# ---------- Locking ----------

async def _lock_order(db: AsyncSession, order_id: UUID) -> Order | None:
    return await db.scalar(
        select(Order)
        .where(Order.id == order_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def _lock_payment(db: AsyncSession, order_id: UUID) -> Payment | None:
    return await db.scalar(
        select(Payment)
        .where(Payment.order_id == order_id, Payment.provider == SIMULATION_PROVIDER)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def _order_tickets(db: AsyncSession, order_id: UUID) -> list[Ticket]:
    rows = await db.execute(
        select(Ticket)
        .join(OrderItem, OrderItem.id == Ticket.order_item_id)
        .where(OrderItem.order_id == order_id)
        .with_for_update(of=Ticket)
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars().all())


# ---------- Shared state transitions ----------

async def _invalidate(
    db: AsyncSession, order: Order, *, ticket_status: TicketStatus, now: datetime
) -> int:
    """Invalidate VALID tickets, release holds and promo capacity."""
    result = await db.execute(
        update(Ticket)
        .where(
            Ticket.order_item_id.in_(
                select(OrderItem.id).where(OrderItem.order_id == order.id)
            ),
            Ticket.status == TicketStatus.VALID,
        )
        .values(status=ticket_status, cancelled_at=now, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    await db.execute(
        update(InventoryHold)
        .where(InventoryHold.order_id == order.id, InventoryHold.status == HoldStatus.ACTIVE)
        .values(status=HoldStatus.RELEASED, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    await campaign_service.release_redemptions(db, [order.id])
    return cast(CursorResult, result).rowcount or 0


async def _create_simulated_refund(
    db: AsyncSession,
    *,
    order: Order,
    payment: Payment,
    initiated_by: UUID | None,
    actor_type: str,
    kind: str,
    reason: str | None,
) -> Refund:
    """Fully refund ``payment``. Caller holds the order and payment locks."""
    existing = await db.scalar(
        select(Refund).where(
            Refund.payment_id == payment.id,
            Refund.status.in_((RefundStatus.PENDING, RefundStatus.SUCCEEDED)),
        )
    )
    if existing is not None:
        return existing

    now = _now()
    refund = Refund(
        payment_id=payment.id,
        order_id=order.id,
        initiated_by=initiated_by,
        amount=payment.amount,  # what was actually charged (after discounts)
        currency=payment.currency,
        reason=reason,
        kind=kind,
        status=RefundStatus.PENDING,
        is_simulated=True,
        created_at=now,
        updated_at=now,
    )
    # No savepoint: the order/payment locks already serialize refunds, and if
    # uq_refunds_active_per_payment ever fires the whole transaction must fail.
    db.add(refund)
    try:
        await db.flush()
    except IntegrityError as exc:
        # Lost a race on uq_refunds_active_per_payment: never pay twice.
        raise ApiError(
            409, "REFUND_IN_PROGRESS", "A refund for this payment already exists",
            retryable=True,
        ) from exc
    audit: dict[str, Any] = {
        "actor_user_id": initiated_by,
        "actor_type": actor_type,
        "event_id": order.event_id,
        "entity_type": "refund",
        "entity_id": refund.id,
    }
    await repo.add_audit_entry(
        db, **audit, action="refund.requested",
        metadata={"order_id": str(order.id), "payment_id": str(payment.id),
                  "amount": str(refund.amount), "currency": refund.currency, "kind": kind},
    )

    # Simulated provider: the refund settles synchronously inside this
    # transaction. A real provider would be called after commit instead.
    refund.status = RefundStatus.SUCCEEDED
    refund.provider_refund_ref = f"sim-refund-{refund.id}"
    refund.completed_at = now
    refund.updated_at = now
    payment.status = PaymentStatus.REFUNDED
    payment.updated_at = now
    await db.flush()
    await repo.add_audit_entry(
        db, **audit, action="refund.completed",
        before_data={"status": RefundStatus.PENDING.value},
        after_data={"status": RefundStatus.SUCCEEDED.value},
        metadata={"amount": str(refund.amount), "currency": refund.currency,
                  "is_simulated": True, "kind": kind},
    )
    await repo.add_audit_entry(
        db, actor_user_id=initiated_by, actor_type=actor_type, event_id=order.event_id,
        entity_type="payment", entity_id=payment.id, action="payment.state_changed",
        before_data={"status": PaymentStatus.SUCCEEDED.value},
        after_data={"status": PaymentStatus.REFUNDED.value},
        metadata={"refund_id": str(refund.id)},
    )
    return refund


async def _cancel_pending(
    db: AsyncSession, order: Order, payment: Payment | None, *, now: datetime
) -> None:
    if payment is not None and payment.status == PaymentStatus.PENDING:
        payment.status = PaymentStatus.CANCELLED
        payment.failure_code = "ORDER_CANCELLED"
        payment.updated_at = now
    await _invalidate(db, order, ticket_status=TicketStatus.CANCELLED, now=now)
    order.status = OrderStatus.CANCELLED
    order.cancelled_at = now
    order.updated_at = now


def _paid(order: Order, payment: Payment | None) -> bool:
    return (
        order.total_amount > 0
        and payment is not None
        and payment.status in (PaymentStatus.SUCCEEDED, PaymentStatus.REFUNDED)
    )


# ---------- Customer cancellation / refund ----------

@dataclass
class CancellationResult:
    order: Order
    refund: Refund | None
    replayed: bool


async def _deny(
    db: AsyncSession, user: User, order: Order, code: str, message: str,
    *, status_code: int = 409,
) -> None:
    """Persist an audit record of the rejected request, then raise."""
    await repo.add_audit_entry(
        db, actor_user_id=user.id, actor_type="CUSTOMER", event_id=order.event_id,
        entity_type="order", entity_id=order.id, action="refund.denied",
        metadata={"reason": code},
    )
    await db.commit()
    raise ApiError(status_code, code, message)


async def customer_cancel(
    db: AsyncSession, user: User, order_id: UUID, *, require_refund: bool = False
) -> CancellationResult:
    """Cancel (free) or refund (paid) the caller's own order.

    Only the buyer may do this (a PLATFORM_ADMIN may act on their behalf).
    Organizers cannot refund individual orders through this path; their
    lever is event cancellation, which has its own documented policy.
    Repeating the request returns the stored outcome with ``replayed=True``.
    """
    order = await _lock_order(db, order_id)
    if order is None:
        raise ApiError(404, "ORDER_NOT_FOUND", "Order not found")
    if order.buyer_user_id != user.id and user.global_role != UserRole.PLATFORM_ADMIN:
        raise ApiError(403, "INSUFFICIENT_PERMISSIONS", "Only the buyer can cancel this order")
    payment = await _lock_payment(db, order.id)
    event = await repo.get_event_by_id(db, order.event_id)
    if event is None:
        raise ApiError(404, "EVENT_NOT_FOUND", "Event not found")
    actor_type = _actor_type(user, event)
    now = _now()

    if order.status in _CLOSED:
        refund = await _refund_for_order(db, order.id)
        if require_refund and refund is None:
            raise ApiError(409, "ORDER_NOT_PAID", "This order has no payment to refund")
        return CancellationResult(order, refund, replayed=True)
    if order.status not in _ACTIVE:
        raise ApiError(
            409, "ORDER_NOT_CANCELLABLE", f"Order cannot be cancelled from {order.status.value}"
        )

    if order.status == OrderStatus.PENDING:
        if require_refund:
            raise ApiError(409, "ORDER_NOT_PAID", "This order has no completed payment to refund")
        await _cancel_pending(db, order, payment, now=now)
        await _audit_order(db, user.id, actor_type, order, OrderStatus.PENDING, "order.cancelled")
        return CancellationResult(order, None, replayed=False)

    if event.status == EventStatus.CANCELLED:
        # The organizer flow owns these orders; process this one right now so
        # the customer sees the documented outcome.
        refund = await _process_order_for_event_cancellation(
            db, order, payment, event=event, actor_user_id=user.id, actor_type="SYSTEM"
        )
        return CancellationResult(order, refund, replayed=False)

    tickets = await _order_tickets(db, order.id)
    if any(ticket.status == TicketStatus.CHECKED_IN for ticket in tickets):
        raise ApiError(409, "TICKET_ALREADY_CHECKED_IN", "A ticket has already been checked in")
    if now >= event.starts_at:
        raise ApiError(409, "EVENT_ALREADY_STARTED", "The event has already started")

    previous = order.status
    if _paid(order, payment):
        assert payment is not None
        violation = _policy_violation(event, now)
        if violation is not None:
            await _deny(db, user, order, *violation)
        refund = await _create_simulated_refund(
            db, order=order, payment=payment, initiated_by=user.id, actor_type=actor_type,
            kind=KIND_CUSTOMER, reason="Customer cancellation",
        )
        await _invalidate(db, order, ticket_status=TicketStatus.REFUNDED, now=now)
        order.status = OrderStatus.REFUNDED
        order.cancelled_at = now
        order.updated_at = now
        await notifications.enqueue(
            db, order_id=order.id, kind=notifications.KIND_REFUND_COMPLETED,
            payload={"refund_amount": f"{refund.amount} {refund.currency}",
                     "refunded_at": now.isoformat(),
                     "notice": "Simulated (demo) refund - no real money was moved."},
        )
        await _audit_order(db, user.id, actor_type, order, previous, "order.refunded",
                           refund_id=refund.id)
        await db.flush()
        return CancellationResult(order, refund, replayed=False)

    if require_refund:
        raise ApiError(409, "ORDER_NOT_PAID", "Free registrations have nothing to refund")
    if order.total_amount > 0:
        raise ApiError(409, "PAYMENT_NOT_FOUND", "No completed payment was found for this order")
    # Free registration: no artificial monetary refund.
    await _invalidate(db, order, ticket_status=TicketStatus.CANCELLED, now=now)
    order.status = OrderStatus.CANCELLED
    order.cancelled_at = now
    order.updated_at = now
    await _audit_order(db, user.id, actor_type, order, previous, "order.cancelled")
    await db.flush()
    return CancellationResult(order, None, replayed=False)


async def _audit_order(
    db: AsyncSession, actor_user_id: UUID | None, actor_type: str, order: Order,
    previous: OrderStatus, action: str, **metadata: Any,
) -> None:
    await repo.add_audit_entry(
        db, actor_user_id=actor_user_id, actor_type=actor_type, event_id=order.event_id,
        entity_type="order", entity_id=order.id, action=action,
        before_data={"status": previous.value}, after_data={"status": order.status.value},
        metadata={key: str(value) for key, value in metadata.items()} or None,
    )


async def _refund_for_order(db: AsyncSession, order_id: UUID) -> Refund | None:
    return await db.scalar(
        select(Refund)
        .where(Refund.order_id == order_id, Refund.status == RefundStatus.SUCCEEDED)
        .order_by(Refund.created_at.desc())
    )


async def list_order_refunds(db: AsyncSession, order_id: UUID) -> list[Refund]:
    rows = await db.execute(
        select(Refund).where(Refund.order_id == order_id).order_by(Refund.created_at)
    )
    return list(rows.scalars().all())


async def refund_eligibility(db: AsyncSession, user: User, order_id: UUID) -> dict[str, Any]:
    """Read-only preview of what POST /cancel or /refund would do."""
    order = await repo.get_order_by_id(db, order_id)
    if order is None:
        raise ApiError(404, "ORDER_NOT_FOUND", "Order not found")
    if order.buyer_user_id != user.id and user.global_role != UserRole.PLATFORM_ADMIN:
        raise ApiError(403, "INSUFFICIENT_PERMISSIONS", "Insufficient permissions")
    event = await repo.get_event_by_id(db, order.event_id)
    assert event is not None
    payment = await db.scalar(
        select(Payment).where(Payment.order_id == order.id,
                              Payment.provider == SIMULATION_PROVIDER)
    )
    now = _now()
    paid = _paid(order, payment)
    reason: str | None = None
    if order.status in _CLOSED:
        reason = "ORDER_ALREADY_CLOSED"
    elif order.status not in _COMMITTED:
        reason = "ORDER_NOT_CONFIRMED"
    elif event.status == EventStatus.CANCELLED:
        reason = None
    elif any(t.status == TicketStatus.CHECKED_IN
             for t in await repo.list_order_tickets(db, order.id)):
        reason = "TICKET_ALREADY_CHECKED_IN"
    elif now >= event.starts_at:
        reason = "EVENT_ALREADY_STARTED"
    elif paid:
        violation = _policy_violation(event, now)
        reason = violation[0] if violation else None
    return {
        "order_id": order.id,
        "cancellable": reason is None,
        "refundable": reason is None and paid,
        "reason": reason,
        "refund_amount": payment.amount if paid and payment is not None else Decimal("0.00"),
        "currency": order.currency,
        **{k: v for k, v in policy_view(event).items() if k != "event_id"},
    }


# ---------- Event cancellation ----------

async def _process_order_for_event_cancellation(
    db: AsyncSession,
    order: Order,
    payment: Payment | None,
    *,
    event: Event,
    actor_user_id: UUID | None,
    actor_type: str,
) -> Refund | None:
    """Close one active order of a cancelled event. Caller holds the locks."""
    now = _now()
    previous = order.status
    if order.status == OrderStatus.PENDING:
        await _cancel_pending(db, order, payment, now=now)
        await _audit_order(db, actor_user_id, actor_type, order, previous, "order.cancelled",
                           cause="event_cancelled")
        return None
    if order.status not in _COMMITTED:
        return None

    refund: Refund | None = None
    if _paid(order, payment):
        assert payment is not None
        refund = await _create_simulated_refund(
            db, order=order, payment=payment, initiated_by=actor_user_id,
            actor_type=actor_type, kind=KIND_EVENT_CANCELLATION,
            reason=event.cancellation_reason or "Event cancelled by organizer",
        )
        await _invalidate(db, order, ticket_status=TicketStatus.REFUNDED, now=now)
        order.status = OrderStatus.REFUNDED
    else:
        await _invalidate(db, order, ticket_status=TicketStatus.CANCELLED, now=now)
        order.status = OrderStatus.CANCELLED
    order.cancelled_at = now
    order.updated_at = now
    # One email per order: the cancellation notice carries the refund amount,
    # so no separate REFUND_COMPLETED email is queued for this path.
    await notifications.enqueue(
        db, order_id=order.id, kind=notifications.KIND_EVENT_CANCELLED,
        payload={
            "reason": event.cancellation_reason,
            "refund_amount": f"{refund.amount} {refund.currency}" if refund else None,
            "cancelled_at": (event.cancelled_at or now).isoformat(),
            "notice": ("A full refund has been issued (simulated demo refund)."
                       if refund else "This was a free registration; no payment to refund."),
        },
    )
    await _audit_order(
        db, actor_user_id, actor_type, order, previous,
        "order.refunded" if refund else "order.cancelled",
        cause="event_cancelled", **({"refund_id": refund.id} if refund else {}),
    )
    await db.flush()
    return refund


async def process_event_cancellation(
    db: AsyncSession,
    event_id: UUID,
    *,
    actor_user_id: UUID | None,
    actor_type: str,
    batch_size: int = 100,
    max_batches: int | None = None,
) -> int:
    """Close every active order of a cancelled event, one committed batch at a time.

    Progress lives in the orders themselves (an order leaves PENDING/PAID/
    CONFIRMED exactly once), so a crash or a retry simply continues with the
    remaining orders. ``SKIP LOCKED`` lets the HTTP request and the background
    sweep work in parallel on disjoint orders. Returns orders processed.
    """
    processed = 0
    batches = 0
    while max_batches is None or batches < max_batches:
        event = await db.get(Event, event_id, populate_existing=True)
        if event is None or event.status != EventStatus.CANCELLED:
            break
        orders = list(
            (
                await db.execute(
                    select(Order)
                    .where(Order.event_id == event_id, Order.status.in_(_ACTIVE))
                    .order_by(Order.created_at, Order.id)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                    .execution_options(populate_existing=True)
                )
            ).scalars().all()
        )
        if not orders:
            break
        for order in orders:
            payment = await _lock_payment(db, order.id)
            await _process_order_for_event_cancellation(
                db, order, payment, event=event,
                actor_user_id=actor_user_id, actor_type=actor_type,
            )
        await db.commit()
        processed += len(orders)
        batches += 1
    return processed


async def cancel_event(
    db: AsyncSession, actor: User, event: Event, *, reason: str | None, batch_size: int
) -> Event:
    """Organizer cancellation. Safe to call again: it resumes unfinished work."""
    if event.status != EventStatus.CANCELLED:
        await db.refresh(event, with_for_update=True)
        locked = event
        if locked.status != EventStatus.CANCELLED:
            now = _now()
            locked.cancelled_at = now
            locked.cancellation_reason = (reason or "").strip() or None
            await event_service.cancel_event(
                db, actor, locked, actor_type=_actor_type(actor, locked),
                metadata={"reason": locked.cancellation_reason},
            )
        await db.commit()
    await process_event_cancellation(
        db, event.id, actor_user_id=actor.id, actor_type=_actor_type(actor, event),
        batch_size=batch_size,
    )
    await db.refresh(event)
    return event


async def resume_event_cancellations(db: AsyncSession, *, batch_size: int) -> int:
    """Background safety net: finish cancellations interrupted mid-way."""
    event_ids = (
        await db.execute(
            select(Order.event_id)
            .join(Event, Event.id == Order.event_id)
            .where(Event.status == EventStatus.CANCELLED, Order.status.in_(_ACTIVE))
            .distinct()
            .limit(10)
        )
    ).scalars().all()
    total = 0
    for event_id in event_ids:
        total += await process_event_cancellation(
            db, event_id, actor_user_id=None, actor_type="SYSTEM",
            batch_size=batch_size, max_batches=5,
        )
    return total


async def cancellation_summary(db: AsyncSession, event: Event) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(Order.status, func.count(Order.id))
            .where(Order.event_id == event.id)
            .group_by(Order.status)
        )
    ).all()
    counts = {status.value: int(count) for status, count in rows}
    refunded_total = await db.scalar(
        select(func.coalesce(func.sum(Refund.amount), 0))
        .join(Order, Order.id == Refund.order_id)
        .where(Order.event_id == event.id, Refund.status == RefundStatus.SUCCEEDED,
               Refund.kind == KIND_EVENT_CANCELLATION)
    )
    remaining = sum(counts.get(status.value, 0) for status in _ACTIVE)
    return {
        "event_id": event.id,
        "event_status": event.status.value,
        "cancelled_at": event.cancelled_at,
        "reason": event.cancellation_reason,
        "orders_remaining": remaining,
        "orders_refunded": counts.get(OrderStatus.REFUNDED.value, 0),
        "orders_cancelled": counts.get(OrderStatus.CANCELLED.value, 0),
        "event_cancellation_refund_total": Decimal(refunded_total or 0).quantize(Decimal("0.01")),
        "complete": event.status == EventStatus.CANCELLED and remaining == 0,
    }
