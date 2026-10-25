from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.models import Attendee, Event, EventStatus, HoldStatus, InventoryHold, Order, OrderItem, OrderStatus, TicketType, User

HOLD_MINUTES = 10

async def expire_holds(db: AsyncSession) -> int:
    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(InventoryHold).where(InventoryHold.status == HoldStatus.ACTIVE,
            InventoryHold.expires_at <= now).values(status=HoldStatus.EXPIRED, updated_at=now)
    )
    await db.execute(update(Order).where(Order.status == OrderStatus.PENDING,
        Order.expires_at <= now).values(status=OrderStatus.EXPIRED, updated_at=now))
    return result.rowcount or 0

async def create_order(db: AsyncSession, user: User, event_id: UUID, requested_items: list) -> Order:
    now = datetime.now(timezone.utc)
    await expire_holds(db)
    event = await db.get(Event, event_id)
    if event is None or event.status != EventStatus.PUBLISHED:
        raise ApiError(404, "EVENT_NOT_AVAILABLE", "Event is not available for registration")

    # Combine duplicate ticket type lines to prevent bypassing per-order limits.
    quantities: dict[UUID, int] = {}
    for item in requested_items:
        quantities[item.ticket_type_id] = quantities.get(item.ticket_type_id, 0) + item.quantity

    prepared = []
    subtotal = Decimal("0.00")
    for ticket_type_id, quantity in sorted(quantities.items(), key=lambda pair: str(pair[0])):
        stmt = select(TicketType).where(TicketType.id == ticket_type_id, TicketType.event_id == event_id).with_for_update()
        ticket_type = (await db.execute(stmt)).scalar_one_or_none()
        if ticket_type is None or ticket_type.is_hidden:
            raise ApiError(400, "INVALID_TICKET_TYPE", "One or more ticket types are unavailable")
        if quantity < ticket_type.min_per_order or quantity > ticket_type.max_per_order:
            raise ApiError(400, "ORDER_LIMIT_EXCEEDED", f"Ticket type '{ticket_type.name}' allows {ticket_type.min_per_order}–{ticket_type.max_per_order} tickets per order", details={"ticket_type_id": str(ticket_type.id), "min": ticket_type.min_per_order, "max": ticket_type.max_per_order})
        if ticket_type.sales_starts_at and now < ticket_type.sales_starts_at:
            raise ApiError(400, "SALES_NOT_STARTED", f"Sales for '{ticket_type.name}' have not started")
        if ticket_type.sales_ends_at and now >= ticket_type.sales_ends_at:
            raise ApiError(400, "SALES_ENDED", f"Sales for '{ticket_type.name}' have ended")
        sold_stmt = select(func.count(OrderItem.id)).join(Order, Order.id == OrderItem.order_id).where(
            OrderItem.ticket_type_id == ticket_type_id, Order.status == OrderStatus.PAID)
        sold = int((await db.execute(sold_stmt)).scalar_one())
        held_stmt = select(func.coalesce(func.sum(InventoryHold.quantity), 0)).where(
            InventoryHold.ticket_type_id == ticket_type_id, InventoryHold.status == HoldStatus.ACTIVE,
            InventoryHold.expires_at > now)
        held = int((await db.execute(held_stmt)).scalar_one())
        if sold + held + quantity > ticket_type.capacity:
            raise ApiError(409, "TICKETS_SOLD_OUT", f"Not enough tickets remaining for '{ticket_type.name}'", retryable=True,
                details={"ticket_type_id": str(ticket_type.id), "available": max(0, ticket_type.capacity - sold - held)})
        line_total = ticket_type.price * quantity
        subtotal += line_total
        prepared.append((ticket_type, quantity))

    is_free = subtotal == 0
    expires_at = None if is_free else now + timedelta(minutes=HOLD_MINUTES)
    order = Order(event_id=event_id, buyer_user_id=user.id, buyer_email=user.email,
        status=OrderStatus.PAID if is_free else OrderStatus.PENDING, currency="KZT",
        subtotal_amount=subtotal, discount_amount=Decimal("0.00"), fee_amount=Decimal("0.00"),
        total_amount=subtotal, expires_at=expires_at, paid_at=now if is_free else None)
    db.add(order)
    await db.flush()
    attendee = Attendee(user_id=user.id, full_name=f"{user.first_name} {user.last_name}".strip(), email=user.email, phone=user.phone)
    db.add(attendee)
    await db.flush()
    for ticket_type, quantity in prepared:
        for _ in range(quantity):
            db.add(OrderItem(order_id=order.id, ticket_type_id=ticket_type.id, attendee_id=attendee.id,
                ticket_type_name=ticket_type.name, unit_price=ticket_type.price,
                discount_amount=Decimal("0.00"), final_price=ticket_type.price))
        if not is_free:
            db.add(InventoryHold(event_id=event_id, ticket_type_id=ticket_type.id, user_id=user.id,
                order_id=order.id, quantity=quantity, status=HoldStatus.ACTIVE, expires_at=expires_at))
    await db.flush()
    return order

async def get_order(db: AsyncSession, order_id: UUID, user: User) -> Order:
    order = await db.get(Order, order_id)
    if order is None or order.buyer_user_id != user.id:
        raise ApiError(404, "ORDER_NOT_FOUND", "Order was not found")
    return order
