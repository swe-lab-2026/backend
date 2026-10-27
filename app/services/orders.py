import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import update
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

DEFAULT_RESERVATION_TTL_MINUTES = 15
_COMMITTED_STATUSES = {OrderStatus.PAID, OrderStatus.CONFIRMED}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_saleable(event: Event, ticket_type: TicketType, now: datetime) -> None:
    if event.status == EventStatus.CANCELLED:
        raise ApiError(409, "EVENT_CANCELLED", "Event is cancelled")
    if event.status != EventStatus.PUBLISHED:
        raise ApiError(409, "EVENT_NOT_ON_SALE", "Event is not on sale")
    if ticket_type.is_hidden:
        raise ApiError(409, "TICKET_TYPE_HIDDEN", "Ticket type is not available")

    registration_start = as_utc(event.registration_starts_at)
    registration_end = as_utc(event.registration_ends_at)
    sales_start = as_utc(ticket_type.sales_starts_at)
    sales_end = as_utc(ticket_type.sales_ends_at)

    if registration_start is not None and now < registration_start:
        raise ApiError(409, "REGISTRATION_NOT_OPEN", "Registration has not opened yet")
    if registration_end is not None and now > registration_end:
        raise ApiError(409, "REGISTRATION_CLOSED", "Registration has closed")
    if sales_start is not None and now < sales_start:
        raise ApiError(409, "SALES_NOT_OPEN", "Ticket sales have not opened yet")
    if sales_end is not None and now > sales_end:
        raise ApiError(409, "SALES_CLOSED", "Ticket sales have ended")


async def _lock_event_and_types(
    db: AsyncSession, event_id: UUID, ticket_type_ids: list[UUID]
) -> tuple[Event, dict[UUID, TicketType]]:
    event = await repo.get_event_for_update(db, event_id)
    if event is None:
        raise ApiError(404, "EVENT_NOT_FOUND", "Event not found")
    locked: dict[UUID, TicketType] = {}
    for ticket_type_id in sorted(ticket_type_ids, key=str):
        ticket_type = await repo.lock_ticket_type(db, ticket_type_id)
        if ticket_type is None or ticket_type.event_id != event_id:
            raise ApiError(404, "TICKET_TYPE_NOT_FOUND", "Ticket type not found")
        locked[ticket_type_id] = ticket_type
    return event, locked


async def _check_availability(
    db: AsyncSession,
    event: Event,
    requested: list[tuple[TicketType, int]],
    now: datetime,
) -> None:
    total_quantity = sum(quantity for _, quantity in requested)
    for ticket_type, quantity in requested:
        if quantity < ticket_type.min_per_order or quantity > ticket_type.max_per_order:
            raise ApiError(
                422,
                "QUANTITY_LIMIT",
                f"Quantity must be between {ticket_type.min_per_order} "
                f"and {ticket_type.max_per_order}",
            )
        sold = await repo.count_sold_items(db, ticket_type.id)
        reserved = await repo.sum_active_holds(db, ticket_type.id, now=now)
        if sold + reserved + quantity > ticket_type.capacity:
            raise ApiError(409, "TICKET_SOLD_OUT", "Selected ticket type is sold out")

    if event.capacity is not None:
        event_sold = await repo.count_event_sold_items(db, event.id)
        event_reserved = await repo.sum_event_active_holds(db, event.id, now=now)
        if event_sold + event_reserved + total_quantity > event.capacity:
            raise ApiError(409, "EVENT_SOLD_OUT", "Event capacity has been reached")


async def create_order(
    db: AsyncSession,
    user: User,
    payload: OrderCreate,
    *,
    qr_secret: str,
    reservation_ttl_minutes: int = DEFAULT_RESERVATION_TTL_MINUTES,
) -> Order:
    quantities: dict[UUID, int] = {}
    for item in payload.items:
        quantities[item.ticket_type_id] = (
            quantities.get(item.ticket_type_id, 0) + item.quantity
        )

    event, locked_types = await _lock_event_and_types(
        db, payload.event_id, list(quantities)
    )
    now = _now()
    requested: list[tuple[TicketType, int]] = []
    for ticket_type_id, quantity in quantities.items():
        ticket_type = locked_types[ticket_type_id]
        _validate_saleable(event, ticket_type, now)
        requested.append((ticket_type, quantity))

    await _check_availability(db, event, requested, now)

    currencies = {ticket_type.currency for ticket_type, _ in requested}
    if len(currencies) != 1:
        raise ApiError(
            409, "CURRENCY_MISMATCH", "Selected ticket types use different currencies"
        )
    currency = next(iter(currencies))

    subtotal = sum(
        (ticket_type.price * quantity for ticket_type, quantity in requested),
        Decimal("0"),
    )
    is_free = subtotal == 0
    status = OrderStatus.CONFIRMED if is_free else OrderStatus.PENDING
    expires_at = (
        None if is_free else now + timedelta(minutes=reservation_ttl_minutes)
    )

    order = Order(
        event_id=event.id,
        buyer_user_id=user.id,
        buyer_email=user.email,
        status=status,
        currency=currency,
        subtotal_amount=subtotal,
        discount_amount=Decimal("0"),
        fee_amount=Decimal("0"),
        total_amount=subtotal,
        expires_at=expires_at,
        paid_at=now if is_free else None,
    )
    db.add(order)
    await db.flush()

    for ticket_type, quantity in requested:
        for _ in range(quantity):
            db.add(
                OrderItem(
                    order_id=order.id,
                    ticket_type_id=ticket_type.id,
                    ticket_type_name=ticket_type.name,
                    unit_price=ticket_type.price,
                    discount_amount=Decimal("0"),
                    final_price=ticket_type.price,
                )
            )
    await db.flush()

    if not is_free:
        for ticket_type, quantity in requested:
            db.add(
                InventoryHold(
                    event_id=event.id,
                    ticket_type_id=ticket_type.id,
                    user_id=user.id,
                    order_id=order.id,
                    quantity=quantity,
                    status=HoldStatus.ACTIVE,
                    expires_at=now + timedelta(minutes=reservation_ttl_minutes),
                )
            )
        await db.flush()

    if is_free:
        await ticket_service.ensure_order_tickets(db, order, qr_secret=qr_secret)
        await notifications.enqueue_order_confirmation(db, order_id=order.id)

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
            409, "ORDER_NOT_PAYABLE", f"Order cannot be confirmed from {order.status.value}"
        )

    now = _now()
    expires_at = as_utc(order.expires_at)
    if expires_at is not None and expires_at <= now:
        raise ApiError(409, "ORDER_EXPIRED", "Order reservation has expired")

    holds = await repo.list_order_holds(db, order.id)
    for hold in holds:
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


async def cancel_order(
    db: AsyncSession, user: User, order_id: UUID
) -> Order:
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
            409, "ORDER_NOT_CANCELLABLE", f"Order cannot be cancelled from {order.status.value}"
        )

    order_tickets = await repo.list_order_tickets(db, order.id)
    if any(ticket.status == TicketStatus.CHECKED_IN for ticket in order_tickets):
        raise ApiError(409, "TICKET_ALREADY_CHECKED_IN", "A ticket has already been checked in")

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


async def expire_pending_orders(db: AsyncSession, *, limit: int = 100) -> int:
    now = _now()
    orders = await repo.list_expired_pending_orders(db, now=now, limit=limit)
    for order in orders:
        for hold in await repo.list_order_holds(db, order.id):
            if hold.status == HoldStatus.ACTIVE:
                hold.status = HoldStatus.EXPIRED
                hold.updated_at = now
        order.status = OrderStatus.EXPIRED
        order.updated_at = now
    return len(orders)


async def get_order_for_view(
    db: AsyncSession, user: User, order_id: UUID
) -> Order:
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
