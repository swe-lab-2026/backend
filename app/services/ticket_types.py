import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db import repositories as repo
from app.db.models import Event, TicketType, User
from app.schemas.events import TicketTypeCreate, TicketTypeUpdate
from app.services.events import as_utc, audit_value

logger = logging.getLogger("app.ticket_types")

_EDITABLE_FIELDS = (
    "name",
    "description",
    "price",
    "currency",
    "capacity",
    "min_per_order",
    "max_per_order",
    "sales_starts_at",
    "sales_ends_at",
    "is_hidden",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_ticket_type(
    db: AsyncSession, event: Event, ticket_type_id: UUID
) -> TicketType:
    ticket_type = await repo.get_ticket_type_for_event(
        db, event_id=event.id, ticket_type_id=ticket_type_id
    )
    if ticket_type is None:
        raise ApiError(404, "TICKET_TYPE_NOT_FOUND", "Ticket type not found")
    return ticket_type


def _validate_order_limits(ticket_type: TicketType) -> None:
    if ticket_type.min_per_order < 1:
        raise ApiError(422, "TICKET_TYPE_INVALID_LIMITS", "min_per_order must be positive")
    if ticket_type.max_per_order < ticket_type.min_per_order:
        raise ApiError(
            422,
            "TICKET_TYPE_INVALID_LIMITS",
            "max_per_order must be >= min_per_order",
        )
    if (
        ticket_type.sales_starts_at is not None
        and ticket_type.sales_ends_at is not None
        and as_utc(ticket_type.sales_ends_at) <= as_utc(ticket_type.sales_starts_at)  # type: ignore[operator]
    ):
        raise ApiError(
            422,
            "TICKET_TYPE_INVALID_SALES_WINDOW",
            "sales_ends_at must be after sales_starts_at",
        )


async def _validate_capacity_floor(db: AsyncSession, ticket_type: TicketType) -> None:
    sold = await repo.count_sold_items(db, ticket_type.id)
    reserved = await repo.sum_active_holds(db, ticket_type.id, now=_now())
    if ticket_type.capacity < sold + reserved:
        raise ApiError(
            409,
            "CAPACITY_BELOW_SOLD",
            "Ticket type capacity cannot be reduced below already sold and reserved quantities",
        )


async def create_ticket_type(
    db: AsyncSession, actor: User, event: Event, payload: TicketTypeCreate
) -> TicketType:
    ticket_type = TicketType(
        event_id=event.id,
        name=payload.name,
        description=payload.description,
        price=payload.price,
        currency=payload.currency,
        capacity=payload.capacity,
        min_per_order=payload.min_per_order,
        max_per_order=payload.max_per_order,
        sales_starts_at=payload.sales_starts_at,
        sales_ends_at=payload.sales_ends_at,
        is_hidden=payload.is_hidden,
    )
    _validate_order_limits(ticket_type)
    db.add(ticket_type)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise ApiError(
            409,
            "TICKET_TYPE_NAME_EXISTS",
            "A ticket type with this name already exists for the event",
        ) from exc

    await repo.add_audit_entry(
        db,
        actor_user_id=actor.id,
        event_id=event.id,
        entity_type="ticket_type",
        entity_id=ticket_type.id,
        action="ticket_type.created",
        after_data={
            "name": ticket_type.name,
            "price": audit_value(ticket_type.price),
            "capacity": ticket_type.capacity,
        },
    )
    logger.info("ticket type %s created for event %s by %s", ticket_type.id, event.id, actor.id)
    return ticket_type


async def update_ticket_type(
    db: AsyncSession,
    actor: User,
    event: Event,
    ticket_type: TicketType,
    payload: TicketTypeUpdate,
) -> TicketType:
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}

    for field in _EDITABLE_FIELDS:
        if field in payload.model_fields_set:
            before[field] = audit_value(getattr(ticket_type, field))
            setattr(ticket_type, field, getattr(payload, field))
            after[field] = audit_value(getattr(ticket_type, field))

    if payload.model_fields_set & set(_EDITABLE_FIELDS):
        _validate_order_limits(ticket_type)
        await _validate_capacity_floor(db, ticket_type)

    ticket_type.updated_at = _now()

    if before or after:
        await repo.add_audit_entry(
            db,
            actor_user_id=actor.id,
            event_id=event.id,
            entity_type="ticket_type",
            entity_id=ticket_type.id,
            action="ticket_type.updated",
            before_data=before or None,
            after_data=after or None,
        )
    logger.info("ticket type %s updated by %s", ticket_type.id, actor.id)
    return ticket_type


async def hide_ticket_type(
    db: AsyncSession, actor: User, event: Event, ticket_type: TicketType
) -> None:
    if not ticket_type.is_hidden:
        ticket_type.is_hidden = True
        ticket_type.updated_at = _now()
        await repo.add_audit_entry(
            db,
            actor_user_id=actor.id,
            event_id=event.id,
            entity_type="ticket_type",
            entity_id=ticket_type.id,
            action="ticket_type.hidden",
            before_data={"is_hidden": False},
            after_data={"is_hidden": True},
        )
        logger.info("ticket type %s hidden by %s", ticket_type.id, actor.id)
