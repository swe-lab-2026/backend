"""Order creation, inventory reservation, and expired checkout cleanup."""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db import repositories as repo
from app.db.models import (
    Attendee,
    Event,
    EventStatus,
    HoldStatus,
    InventoryHold,
    Order,
    OrderItem,
    OrderStatus,
    Ticket,
    TicketType,
    User,
)

# Checkout reservations expire after ten minutes. Keep this in sync with the
# single-ticket purchase endpoint in app.services.inventory.
HOLD_MINUTES = 10


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalize database datetimes; SQLite may return TIMESTAMPTZ as naive."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _qr_token_hash() -> str:
    """Store only a digest of a random, unguessable ticket token."""
    token = secrets.token_urlsafe(32)
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def expire_holds(db: AsyncSession) -> int:
    """Release expired holds and mark their pending orders expired.

    Expiration is also checked during availability calculations by timestamp,
    so a delayed cleanup task can never make an expired hold block inventory.
    The function is called by the background cleanup worker and order reads.
    """
    now = _now()
    result = await db.execute(
        update(InventoryHold)
        .where(
            InventoryHold.status == HoldStatus.ACTIVE,
            InventoryHold.expires_at <= now,
        )
        .values(status=HoldStatus.EXPIRED, updated_at=now)
    )
    await db.execute(
        update(Order)
        .where(
            Order.status == OrderStatus.PENDING,
            Order.expires_at.is_not(None),
            Order.expires_at <= now,
        )
        .values(status=OrderStatus.EXPIRED, updated_at=now)
    )
    return result.rowcount or 0


async def _lock_event(db: AsyncSession, event_id: UUID) -> Event:
    """Serialize checkout attempts for an event on PostgreSQL.

    Locking the event row (rather than only a ticket-type row) also protects
    the aggregate event capacity when an order contains multiple ticket types.
    Inventory endpoints acquire the same lock before checking availability.
    """
    stmt = select(Event).where(Event.id == event_id).with_for_update()
    event = (await db.execute(stmt)).scalar_one_or_none()
    if event is None or event.status != EventStatus.PUBLISHED:
        raise ApiError(
            404,
            "EVENT_NOT_AVAILABLE",
            "Event is not available for registration",
        )
    return event


async def _count_sold_for_ticket_type(db: AsyncSession, ticket_type_id: UUID) -> int:
    stmt = (
        select(func.count(OrderItem.id))
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            OrderItem.ticket_type_id == ticket_type_id,
            Order.status == OrderStatus.PAID,
        )
    )
    return int((await db.execute(stmt)).scalar_one())


async def _count_held_for_ticket_type(
    db: AsyncSession, ticket_type_id: UUID, now: datetime
) -> int:
    stmt = select(func.coalesce(func.sum(InventoryHold.quantity), 0)).where(
        InventoryHold.ticket_type_id == ticket_type_id,
        InventoryHold.status == HoldStatus.ACTIVE,
        InventoryHold.expires_at > now,
    )
    return int((await db.execute(stmt)).scalar_one())


async def _count_event_sold(db: AsyncSession, event_id: UUID) -> int:
    stmt = (
        select(func.count(OrderItem.id))
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.event_id == event_id, Order.status == OrderStatus.PAID)
    )
    return int((await db.execute(stmt)).scalar_one())


async def _count_event_held(db: AsyncSession, event_id: UUID, now: datetime) -> int:
    stmt = select(func.coalesce(func.sum(InventoryHold.quantity), 0)).where(
        InventoryHold.event_id == event_id,
        InventoryHold.status == HoldStatus.ACTIVE,
        InventoryHold.expires_at > now,
    )
    return int((await db.execute(stmt)).scalar_one())


async def create_order(
    db: AsyncSession,
    user: User,
    event_id: UUID,
    requested_items: list[Any],
) -> Order:
    """Create an order and atomically reserve requested ticket quantities.

    Free orders are confirmed immediately and receive valid ticket records.
    Paid orders stay PENDING and retain inventory for HOLD_MINUTES while the
    buyer completes checkout. Each ticket unit is represented by one order item.
    """
    await expire_holds(db)
    event = await _lock_event(db, event_id)
    # Refresh the clock after a possible wait on the event row lock.
    now = _now()

    registration_start = _as_utc(event.registration_starts_at)
    registration_end = _as_utc(event.registration_ends_at)
    if registration_start is not None and now < registration_start:
        raise ApiError(409, "REGISTRATION_NOT_OPEN", "Registration has not opened yet")
    if registration_end is not None and now >= registration_end:
        raise ApiError(409, "REGISTRATION_CLOSED", "Registration has closed")

    # Combine duplicate ticket type lines so splitting lines cannot bypass the
    # per-order limit. Lock types in a stable order to avoid deadlocks.
    quantities: dict[UUID, int] = {}
    for item in requested_items:
        quantities[item.ticket_type_id] = quantities.get(item.ticket_type_id, 0) + item.quantity

    prepared: list[tuple[TicketType, int]] = []
    subtotal = Decimal("0.00")
    for ticket_type_id, quantity in sorted(quantities.items(), key=lambda pair: str(pair[0])):
        stmt = (
            select(TicketType)
            .where(
                TicketType.id == ticket_type_id,
                TicketType.event_id == event_id,
            )
            .with_for_update()
        )
        ticket_type = (await db.execute(stmt)).scalar_one_or_none()
        if ticket_type is None or ticket_type.is_hidden:
            raise ApiError(
                400,
                "INVALID_TICKET_TYPE",
                "One or more ticket types are unavailable",
            )

        if quantity < ticket_type.min_per_order or quantity > ticket_type.max_per_order:
            raise ApiError(
                400,
                "ORDER_LIMIT_EXCEEDED",
                f"Ticket type '{ticket_type.name}' allows {ticket_type.min_per_order}–"
                f"{ticket_type.max_per_order} tickets per order",
                details={
                    "ticket_type_id": str(ticket_type.id),
                    "min": ticket_type.min_per_order,
                    "max": ticket_type.max_per_order,
                    "requested": quantity,
                },
            )

        sales_start = _as_utc(ticket_type.sales_starts_at)
        sales_end = _as_utc(ticket_type.sales_ends_at)
        if sales_start is not None and now < sales_start:
            raise ApiError(
                400,
                "SALES_NOT_STARTED",
                f"Sales for '{ticket_type.name}' have not started",
            )
        if sales_end is not None and now >= sales_end:
            raise ApiError(
                400,
                "SALES_ENDED",
                f"Sales for '{ticket_type.name}' have ended",
            )

        sold = await _count_sold_for_ticket_type(db, ticket_type.id)
        held = await _count_held_for_ticket_type(db, ticket_type.id, now)
        if sold + held + quantity > ticket_type.capacity:
            raise ApiError(
                409,
                "TICKETS_SOLD_OUT",
                f"Not enough tickets remaining for '{ticket_type.name}'",
                retryable=True,
                details={
                    "ticket_type_id": str(ticket_type.id),
                    "available": max(0, ticket_type.capacity - sold - held),
                },
            )

        subtotal += ticket_type.price * quantity
        prepared.append((ticket_type, quantity))

    # Enforce the event-wide capacity too: per-type capacity alone is not
    # sufficient when several ticket types share one venue limit.
    event_requested = sum(quantity for _ticket_type, quantity in prepared)
    event_capacity = event.capacity
    if event_capacity is not None:
        event_sold = await _count_event_sold(db, event.id)
        event_held = await _count_event_held(db, event.id, now)
        if event_sold + event_held + event_requested > event_capacity:
            raise ApiError(
                409,
                "EVENT_SOLD_OUT",
                "Event capacity has been reached",
                retryable=True,
                details={
                    "available": max(0, event_capacity - event_sold - event_held),
                    "requested": event_requested,
                },
            )

    is_free = subtotal == Decimal("0.00")
    expires_at = None if is_free else now + timedelta(minutes=HOLD_MINUTES)
    order = Order(
        event_id=event_id,
        buyer_user_id=user.id,
        buyer_email=user.email,
        status=OrderStatus.PAID if is_free else OrderStatus.PENDING,
        currency="KZT",
        subtotal_amount=subtotal,
        discount_amount=Decimal("0.00"),
        fee_amount=Decimal("0.00"),
        total_amount=subtotal,
        expires_at=expires_at,
        paid_at=now if is_free else None,
    )
    db.add(order)
    await db.flush()

    # One attendee per registration/order. Only paid orders are exposed through
    # the organizer's attendee endpoint, so abandoned paid checkout attempts do
    # not appear in the confirmed attendee list.
    attendee = Attendee(
        user_id=user.id,
        full_name=f"{user.first_name} {user.last_name}".strip(),
        email=user.email,
        phone=user.phone,
    )
    db.add(attendee)
    await db.flush()

    order_items: list[OrderItem] = []
    for ticket_type, quantity in prepared:
        for _ in range(quantity):
            order_items.append(
                OrderItem(
                    order_id=order.id,
                    ticket_type_id=ticket_type.id,
                    attendee_id=attendee.id,
                    ticket_type_name=ticket_type.name,
                    unit_price=ticket_type.price,
                    discount_amount=Decimal("0.00"),
                    final_price=ticket_type.price,
                )
            )
    db.add_all(order_items)
    await db.flush()

    if is_free:
        # A free order is already confirmed, so its tickets are issued now and
        # do not need an inventory hold.
        db.add_all(
            [Ticket(order_item_id=item.id, qr_token_hash=_qr_token_hash()) for item in order_items]
        )
    else:
        assert expires_at is not None
        db.add_all(
            [
                InventoryHold(
                    event_id=event_id,
                    ticket_type_id=ticket_type.id,
                    user_id=user.id,
                    order_id=order.id,
                    quantity=quantity,
                    status=HoldStatus.ACTIVE,
                    expires_at=expires_at,
                )
                for ticket_type, quantity in prepared
            ]
        )

    await repo.add_audit_entry(
        db,
        actor_user_id=user.id,
        event_id=event.id,
        entity_type="order",
        entity_id=order.id,
        action="order.created",
        after_data={
            "status": order.status.value,
            "total_amount": str(order.total_amount),
            "is_free": is_free,
            "items": [
                {"ticket_type_id": str(ticket_type.id), "quantity": quantity}
                for ticket_type, quantity in prepared
            ],
        },
    )
    return order


async def list_event_attendees(
    db: AsyncSession,
    event_id: UUID,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List unique attendees from confirmed orders for an organizer-owned event."""
    stmt = (
        select(
            Attendee.id.label("attendee_id"),
            Attendee.user_id.label("user_id"),
            Attendee.full_name.label("full_name"),
            Attendee.email.label("email"),
            Attendee.phone.label("phone"),
            Attendee.created_at.label("registered_at"),
            Order.id.label("order_id"),
            Order.order_number.label("order_number"),
            Order.currency.label("currency"),
            Order.total_amount.label("total_amount"),
            func.count(OrderItem.id).label("ticket_count"),
        )
        .select_from(Order)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .join(Attendee, Attendee.id == OrderItem.attendee_id)
        .where(Order.event_id == event_id, Order.status == OrderStatus.PAID)
        .group_by(
            Attendee.id,
            Attendee.user_id,
            Attendee.full_name,
            Attendee.email,
            Attendee.phone,
            Attendee.created_at,
            Order.id,
            Order.order_number,
            Order.currency,
            Order.total_amount,
        )
        .order_by(Attendee.created_at.desc(), Order.id.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(stmt)
    return [dict(row) for row in result.mappings().all()]


async def get_order(db: AsyncSession, order_id: UUID, user: User) -> Order:
    order = await db.get(Order, order_id)
    if order is None or order.buyer_user_id != user.id:
        raise ApiError(404, "ORDER_NOT_FOUND", "Order was not found")
    return order
