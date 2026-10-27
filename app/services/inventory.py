import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ApiError
from app.db import repositories as repo
from app.db.models import (
    Event,
    EventStatus,
    HoldStatus,
    InventoryHold,
    TicketStatus,
    TicketType,
    User,
)
from app.schemas.events import (
    EventInventoryResponse,
    HoldResponse,
    PurchaseRequest,
    PurchaseResponse,
    TicketItemResponse,
    TicketTypeInventory,
)
from app.services.events import as_utc

logger = logging.getLogger("app.inventory")

DEFAULT_HOLD_TTL_MINUTES = 10


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_event_inventory(db: AsyncSession, event: Event) -> EventInventoryResponse:
    now = _now()
    ticket_types = await repo.list_ticket_types(db, event.id)
    items: list[TicketTypeInventory] = []
    total_sold = 0
    total_reserved = 0
    total_available = 0

    for ticket_type in ticket_types:
        sold = await repo.count_sold_items(db, ticket_type.id)
        reserved = await repo.sum_active_holds(db, ticket_type.id, now=now)
        returned = await repo.count_tickets_by_status(
            db, ticket_type.id, status=TicketStatus.REFUNDED
        )
        checked_in = await repo.count_tickets_by_status(
            db, ticket_type.id, status=TicketStatus.CHECKED_IN
        )
        available = max(ticket_type.capacity - sold - reserved, 0)
        total_sold += sold
        total_reserved += reserved
        total_available += available
        items.append(
            TicketTypeInventory(
                ticket_type_id=ticket_type.id,
                name=ticket_type.name,
                is_hidden=ticket_type.is_hidden,
                capacity=ticket_type.capacity,
                reserved=reserved,
                sold=sold,
                returned=returned,
                checked_in=checked_in,
                available=available,
            )
        )

    return EventInventoryResponse(
        event_id=event.id,
        event_capacity=event.capacity,
        total_sold=total_sold,
        total_reserved=total_reserved,
        total_available=total_available,
        ticket_types=items,
    )


async def _lock_event_and_type(
    db: AsyncSession, event_id: UUID, ticket_type_id: UUID
) -> tuple[Event, TicketType]:
    event = await repo.get_event_for_update(db, event_id)
    if event is None:
        raise ApiError(404, "EVENT_NOT_FOUND", "Event not found")
    ticket_type = await repo.lock_ticket_type(db, ticket_type_id)
    if ticket_type is None or ticket_type.event_id != event_id:
        raise ApiError(404, "TICKET_TYPE_NOT_FOUND", "Ticket type not found")
    return event, ticket_type


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


async def _check_availability(
    db: AsyncSession, event: Event, ticket_type: TicketType, quantity: int, now: datetime
) -> None:
    if quantity < ticket_type.min_per_order or quantity > ticket_type.max_per_order:
        raise ApiError(
            422,
            "QUANTITY_LIMIT",
            f"Quantity must be between {ticket_type.min_per_order} and {ticket_type.max_per_order}",
        )

    sold = await repo.count_sold_items(db, ticket_type.id)
    reserved = await repo.sum_active_holds(db, ticket_type.id, now=now)
    if sold + reserved + quantity > ticket_type.capacity:
        raise ApiError(409, "TICKET_SOLD_OUT", "Selected ticket type is sold out")

    if event.capacity is not None:
        event_sold = await repo.count_event_sold_items(db, event.id)
        event_reserved = await repo.sum_event_active_holds(db, event.id, now=now)
        if event_sold + event_reserved + quantity > event.capacity:
            raise ApiError(409, "EVENT_SOLD_OUT", "Event capacity has been reached")


async def create_hold(
    db: AsyncSession,
    user: User,
    event_id: UUID,
    ticket_type_id: UUID,
    *,
    quantity: int,
    expires_in_minutes: int,
) -> HoldResponse:
    event, ticket_type = await _lock_event_and_type(db, event_id, ticket_type_id)
    now = _now()
    _validate_saleable(event, ticket_type, now)
    await _check_availability(db, event, ticket_type, quantity, now)

    hold = InventoryHold(
        event_id=event.id,
        ticket_type_id=ticket_type.id,
        user_id=user.id,
        order_id=None,
        quantity=quantity,
        status=HoldStatus.ACTIVE,
        expires_at=now + timedelta(minutes=expires_in_minutes),
    )
    db.add(hold)
    await db.flush()
    logger.info("hold %s created for event %s by %s", hold.id, event.id, user.id)
    return HoldResponse(
        hold_id=hold.id,
        event_id=event.id,
        ticket_type_id=ticket_type.id,
        quantity=hold.quantity,
        status=hold.status.value,
        expires_at=hold.expires_at,
        created_at=hold.created_at,
    )


async def purchase(
    db: AsyncSession,
    user: User,
    event_id: UUID,
    ticket_type_id: UUID,
    payload: PurchaseRequest,
    *,
    settings: Settings | None = None,
) -> PurchaseResponse:
    """Legacy single-ticket-type checkout.

    Kept for backwards compatibility; it delegates to the order service so
    free registrations, QR credentials, attendees and confirmation emails
    behave exactly like multi-item ``POST /orders``.
    """
    from app.core.config import get_settings
    from app.schemas.orders import OrderCreate, OrderItemCreate
    from app.services import orders as order_service

    event, ticket_type = await _lock_event_and_type(db, event_id, ticket_type_id)
    now = _now()
    _validate_saleable(event, ticket_type, now)
    await _check_availability(db, event, ticket_type, payload.quantity, now)

    settings = settings or get_settings()
    order = await order_service.create_order(
        db,
        user,
        OrderCreate(
            event_id=event_id,
            items=[
                OrderItemCreate(
                    ticket_type_id=ticket_type_id, quantity=payload.quantity
                )
            ],
        ),
        qr_secret=settings.effective_ticket_qr_secret,
        reservation_ttl_minutes=settings.order_reservation_ttl_minutes,
    )

    order_tickets = await repo.list_order_tickets(db, order.id)
    tickets = [
        TicketItemResponse(
            ticket_id=ticket.id,
            public_id=ticket.public_id,
            status=ticket.status.value,
            issued_at=ticket.issued_at,
        )
        for ticket in order_tickets
    ]
    hold: HoldResponse | None = None
    if order.total_amount > 0:
        order_holds = await repo.list_order_holds(db, order.id)
        if order_holds:
            inventory_hold = order_holds[0]
            hold = HoldResponse(
                hold_id=inventory_hold.id,
                event_id=inventory_hold.event_id,
                ticket_type_id=inventory_hold.ticket_type_id,
                quantity=inventory_hold.quantity,
                status=inventory_hold.status.value,
                expires_at=inventory_hold.expires_at,
                created_at=inventory_hold.created_at,
            )

    return PurchaseResponse(
        order_id=order.id,
        order_number=order.order_number,
        status=order.status.value,
        currency=order.currency,
        total_amount=order.total_amount,
        is_free=order.total_amount == 0,
        tickets=tickets,
        hold=hold,
    )
