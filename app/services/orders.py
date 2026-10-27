import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, func, select, update
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
    TicketStatus,
    TicketType,
    User,
    UserRole,
)
from app.schemas.orders import OrderCreate, OrderItemResponse, OrderResponse
from app.schemas.tickets import TicketResponse
from app.services import notifications
from app.services import tickets as ticket_service
from app.services.events import as_utc

logger = logging.getLogger("app.orders")

DEFAULT_RESERVATION_TTL_MINUTES = 10
_COMMITTED_STATUSES = {OrderStatus.PAID, OrderStatus.CONFIRMED}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def expire_holds(db: AsyncSession) -> int:
    """Release expired holds and mark their pending orders expired."""
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
    return cast(CursorResult, result).rowcount or 0


async def expire_pending_orders(db: AsyncSession, *, limit: int = 100) -> int:
    del limit
    return await expire_holds(db)


async def _lock_event(db: AsyncSession, event_id: UUID) -> Event:
    stmt = select(Event).where(Event.id == event_id).with_for_update()
    event = (await db.execute(stmt)).scalar_one_or_none()
    if event is None or event.status != EventStatus.PUBLISHED:
        raise ApiError(
            404,
            "EVENT_NOT_AVAILABLE",
            "Event is not available for registration",
        )
    return event


async def _lock_ticket_type(
    db: AsyncSession, *, event_id: UUID, ticket_type_id: UUID
) -> TicketType:
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
    return ticket_type


async def create_order(
    db: AsyncSession,
    user: User,
    payload: OrderCreate | UUID,
    requested_items: list[Any] | None = None,
    *,
    qr_secret: str = "",
    reservation_ttl_minutes: int = DEFAULT_RESERVATION_TTL_MINUTES,
) -> Order:
    """Create an order and atomically reserve requested ticket quantities.

    Free orders are confirmed immediately and receive valid ticket records and
    a confirmation email. Paid orders stay PENDING and retain inventory for
    ``reservation_ttl_minutes`` while the buyer completes checkout.
    """
    if requested_items is not None:
        event_id = payload if isinstance(payload, UUID) else payload.event_id
        items = requested_items
    else:
        assert not isinstance(payload, UUID)
        event_id = payload.event_id
        items = payload.items

    await expire_holds(db)
    event = await _lock_event(db, event_id)
    now = _now()

    registration_start = as_utc(event.registration_starts_at)
    registration_end = as_utc(event.registration_ends_at)
    if registration_start is not None and now < registration_start:
        raise ApiError(409, "REGISTRATION_NOT_OPEN", "Registration has not opened yet")
    if registration_end is not None and now >= registration_end:
        raise ApiError(409, "REGISTRATION_CLOSED", "Registration has closed")

    quantities: dict[UUID, int] = {}
    for item in items:
        quantities[item.ticket_type_id] = (
            quantities.get(item.ticket_type_id, 0) + item.quantity
        )

    prepared: list[tuple[TicketType, int]] = []
    subtotal = Decimal("0.00")
    currencies: set[str] = set()
    for ticket_type_id, quantity in sorted(
        quantities.items(), key=lambda pair: str(pair[0])
    ):
        ticket_type = await _lock_ticket_type(
            db, event_id=event_id, ticket_type_id=ticket_type_id
        )
        if quantity < ticket_type.min_per_order or quantity > ticket_type.max_per_order:
            raise ApiError(
                400,
                "ORDER_LIMIT_EXCEEDED",
                f"Ticket type '{ticket_type.name}' allows {ticket_type.min_per_order}-"
                f"{ticket_type.max_per_order} tickets per order",
                details={
                    "ticket_type_id": str(ticket_type.id),
                    "min": ticket_type.min_per_order,
                    "max": ticket_type.max_per_order,
                    "requested": quantity,
                },
            )

        sales_start = as_utc(ticket_type.sales_starts_at)
        sales_end = as_utc(ticket_type.sales_ends_at)
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

        sold = await repo.count_sold_items(db, ticket_type.id)
        held = await repo.sum_active_holds(db, ticket_type.id, now=now)
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
        currencies.add(ticket_type.currency)
        prepared.append((ticket_type, quantity))

    if len(currencies) > 1:
        raise ApiError(
            409, "CURRENCY_MISMATCH", "Selected ticket types use different currencies"
        )

    event_requested = sum(quantity for _ticket_type, quantity in prepared)
    if event.capacity is not None:
        event_sold = await repo.count_event_sold_items(db, event.id)
        event_held = await repo.sum_event_active_holds(db, event.id, now=now)
        if event_sold + event_held + event_requested > event.capacity:
            raise ApiError(
                409,
                "EVENT_SOLD_OUT",
                "Event capacity has been reached",
                retryable=True,
                details={
                    "available": max(0, event.capacity - event_sold - event_held),
                    "requested": event_requested,
                },
            )

    is_free = subtotal == Decimal("0.00")
    expires_at = (
        None
        if is_free
        else now + timedelta(minutes=max(1, reservation_ttl_minutes))
    )
    currency = "KZT" if is_free else next(iter(currencies))
    order = Order(
        event_id=event_id,
        buyer_user_id=user.id,
        buyer_email=user.email,
        status=OrderStatus.PAID if is_free else OrderStatus.PENDING,
        currency=currency,
        subtotal_amount=subtotal,
        discount_amount=Decimal("0.00"),
        fee_amount=Decimal("0.00"),
        total_amount=subtotal,
        expires_at=expires_at,
        paid_at=now if is_free else None,
    )
    db.add(order)
    await db.flush()

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
        secret = qr_secret
        if not secret:
            from app.core.config import get_settings

            secret = get_settings().effective_ticket_qr_secret
        await ticket_service.ensure_order_tickets(db, order, qr_secret=secret)
        await notifications.enqueue_order_confirmation(db, order_id=order.id)
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
    logger.info("order %s created for event %s by %s", order.id, event.id, user.id)
    return order


async def confirm_order(
    db: AsyncSession,
    user: User,
    order_id: UUID,
    *,
    qr_secret: str,
) -> Order:
    order = await repo.get_order_for_update(db, order_id)
    if order is None:
        raise ApiError(404, "ORDER_NOT_FOUND", "Order not found")
    if order.buyer_user_id != user.id and user.global_role != UserRole.PLATFORM_ADMIN:
        raise ApiError(403, "INSUFFICIENT_PERMISSIONS", "Insufficient permissions")
    if order.status in _COMMITTED_STATUSES:
        return order
    if order.status != OrderStatus.PENDING:
        raise ApiError(
            409,
            "ORDER_NOT_PAYABLE",
            f"Order cannot be confirmed from {order.status.value}",
        )

    now = _now()
    expires_at = as_utc(order.expires_at)
    if expires_at is not None and expires_at <= now:
        raise ApiError(409, "ORDER_EXPIRED", "Order reservation has expired")

    for hold in await repo.list_order_holds(db, order.id):
        if hold.status == HoldStatus.ACTIVE:
            hold.status = HoldStatus.CONSUMED
            hold.updated_at = now

    order.status = OrderStatus.PAID
    order.paid_at = now
    order.expires_at = None
    order.updated_at = now
    await db.flush()

    await ticket_service.ensure_order_tickets(db, order, qr_secret=qr_secret)
    await notifications.enqueue_order_confirmation(db, order_id=order.id)
    await repo.add_audit_entry(
        db,
        actor_user_id=user.id,
        event_id=order.event_id,
        entity_type="order",
        entity_id=order.id,
        action="order.paid",
        after_data={"status": order.status.value},
    )
    return order


async def cancel_order(db: AsyncSession, user: User, order_id: UUID) -> Order:
    order = await repo.get_order_for_update(db, order_id)
    if order is None:
        raise ApiError(404, "ORDER_NOT_FOUND", "Order not found")
    event = await repo.get_event_by_id(db, order.event_id)
    if event is None:
        raise ApiError(404, "EVENT_NOT_FOUND", "Event not found")

    allowed = (
        order.buyer_user_id == user.id
        or event.owner_user_id == user.id
        or user.global_role == UserRole.PLATFORM_ADMIN
    )
    if not allowed:
        raise ApiError(403, "INSUFFICIENT_PERMISSIONS", "Insufficient permissions")
    if order.status not in {OrderStatus.PENDING} | _COMMITTED_STATUSES:
        raise ApiError(
            409,
            "ORDER_NOT_CANCELLABLE",
            f"Order cannot be cancelled from {order.status.value}",
        )

    order_tickets = await repo.list_order_tickets(db, order.id)
    if any(ticket.status == TicketStatus.CHECKED_IN for ticket in order_tickets):
        raise ApiError(
            409, "TICKET_ALREADY_CHECKED_IN", "A ticket has already been checked in"
        )

    now = _now()
    if order_tickets:
        await db.execute(
            update(Ticket)
            .where(
                Ticket.id.in_([ticket.id for ticket in order_tickets]),
                Ticket.status == TicketStatus.VALID,
            )
            .values(
                status=TicketStatus.CANCELLED,
                cancelled_at=now,
                updated_at=now,
            )
        )

    for hold in await repo.list_order_holds(db, order.id):
        if hold.status == HoldStatus.ACTIVE:
            hold.status = HoldStatus.RELEASED
            hold.updated_at = now

    order.status = OrderStatus.CANCELLED
    order.cancelled_at = now
    order.updated_at = now
    await db.flush()
    await repo.add_audit_entry(
        db,
        actor_user_id=user.id,
        event_id=order.event_id,
        entity_type="order",
        entity_id=order.id,
        action="order.cancelled",
        after_data={"status": order.status.value},
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
        .where(Order.event_id == event_id, Order.status.in_(tuple(_COMMITTED_STATUSES)))
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


async def get_order_for_view(db: AsyncSession, user: User, order_id: UUID) -> Order:
    order = await repo.get_order_by_id(db, order_id)
    if order is None:
        raise ApiError(404, "ORDER_NOT_FOUND", "Order not found")
    event = await repo.get_event_by_id(db, order.event_id)
    if event is None:
        raise ApiError(404, "EVENT_NOT_FOUND", "Event not found")
    allowed = (
        order.buyer_user_id == user.id
        or event.owner_user_id == user.id
        or user.global_role == UserRole.PLATFORM_ADMIN
    )
    if not allowed:
        raise ApiError(403, "INSUFFICIENT_PERMISSIONS", "Insufficient permissions")
    return order


async def build_order_response(
    db: AsyncSession,
    order: Order,
    *,
    qr_secret: str,
    download_base: str,
) -> OrderResponse:
    items = await repo.list_order_items(db, order.id)
    grouped: dict[UUID, list[OrderItem]] = {}
    for item in items:
        grouped.setdefault(item.ticket_type_id, []).append(item)
    item_responses = [
        OrderItemResponse(
            ticket_type_id=ticket_type_id,
            ticket_type_name=rows[0].ticket_type_name,
            quantity=len(rows),
            unit_price=rows[0].unit_price,
            subtotal=sum((row.final_price for row in rows), Decimal("0")),
            final_price=rows[0].final_price,
        )
        for ticket_type_id, rows in grouped.items()
    ]

    tickets: list[TicketResponse] = []
    if order.status in _COMMITTED_STATUSES:
        tickets = await ticket_service.list_order_ticket_responses(
            db, order, qr_secret=qr_secret, download_base=download_base
        )

    return OrderResponse(
        id=order.id,
        order_id=order.id,
        order_number=order.order_number,
        event_id=order.event_id,
        buyer_user_id=order.buyer_user_id,
        buyer_email=order.buyer_email,
        status=order.status.value,
        currency=order.currency,
        subtotal_amount=order.subtotal_amount,
        discount_amount=order.discount_amount,
        fee_amount=order.fee_amount,
        total_amount=order.total_amount,
        is_free=order.total_amount == 0,
        expires_at=order.expires_at,
        paid_at=order.paid_at,
        cancelled_at=order.cancelled_at,
        created_at=order.created_at,
        updated_at=order.updated_at,
        items=item_responses,
        tickets=tickets,
    )
