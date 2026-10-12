import logging
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db import repositories as repo
from app.db.models import (
    Event,
    EventStatus,
    EventVisibility,
    TicketType,
    User,
    UserRole,
)
from app.schemas.events import EventCreate, EventImageInput, EventUpdate

logger = logging.getLogger("app.events")

_ALLOWED_TRANSITIONS: dict[EventStatus, set[EventStatus]] = {
    EventStatus.DRAFT: {EventStatus.PUBLISHED, EventStatus.CANCELLED},
    EventStatus.PUBLISHED: {EventStatus.UNPUBLISHED, EventStatus.CANCELLED},
    EventStatus.UNPUBLISHED: {EventStatus.PUBLISHED, EventStatus.CANCELLED},
    EventStatus.CANCELLED: set(),
    EventStatus.COMPLETED: set(),
}

_PUBLIC_VISIBLE_STATUSES = {
    EventStatus.PUBLISHED,
    EventStatus.CANCELLED,
    EventStatus.COMPLETED,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def audit_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def as_utc(value: datetime | None) -> datetime | None:
    """Normalize a database datetime to timezone-aware UTC.

    TIMESTAMPTZ is returned naive by the SQLite test fallback (which has no
    timezone support); values are still stored as UTC, so a naive value is
    treated as UTC.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _require_organizer_profile(db: AsyncSession, user: User) -> None:
    profile = await repo.get_organizer_profile(db, user.id)
    if profile is None:
        raise ApiError(
            403,
            "ORGANIZER_PROFILE_REQUIRED",
            "An organizer profile is required to manage events",
        )


def is_authorized_viewer(event: Event, user: User | None) -> bool:
    if user is None:
        return False
    if user.global_role == UserRole.PLATFORM_ADMIN:
        return True
    return event.owner_user_id == user.id


def is_publicly_visible(event: Event) -> bool:
    return (
        event.status in _PUBLIC_VISIBLE_STATUSES
        and event.visibility != EventVisibility.PRIVATE
    )


async def create_event(db: AsyncSession, user: User, payload: EventCreate) -> Event:
    await _require_organizer_profile(db, user)

    event = Event(
        owner_user_id=user.id,
        title=payload.title,
        description=payload.description,
        category=payload.category,
        visibility=payload.visibility,
        status=EventStatus.DRAFT,
        venue_name=payload.venue_name,
        venue_address=payload.venue_address,
        city=payload.city,
        country_code=payload.country_code,
        latitude=payload.latitude,
        longitude=payload.longitude,
        timezone=payload.timezone,
        starts_at=payload.starts_at,
        ends_at=payload.ends_at,
        registration_starts_at=payload.registration_starts_at,
        registration_ends_at=payload.registration_ends_at,
        capacity=payload.capacity,
        refund_policy=payload.refund_policy,
    )
    db.add(event)
    await db.flush()

    for image in payload.images:
        await repo.create_event_image(
            db,
            event_id=event.id,
            object_key=image.object_key,
            alt_text=image.alt_text,
            sort_order=image.sort_order,
        )

    await repo.add_audit_entry(
        db,
        actor_user_id=user.id,
        event_id=event.id,
        entity_type="event",
        entity_id=event.id,
        action="event.created",
        after_data={"title": event.title, "status": event.status.value},
    )
    logger.info("event %s created by %s", event.id, user.id)
    return event


async def get_visible_event(db: AsyncSession, event_id: UUID, user: User | None) -> Event:
    event = await repo.get_event_by_id(db, event_id)
    if event is None:
        raise ApiError(404, "EVENT_NOT_FOUND", "Event not found")
    if is_authorized_viewer(event, user):
        return event
    if not is_publicly_visible(event):
        raise ApiError(404, "EVENT_NOT_FOUND", "Event not found")
    return event


async def list_owner_events(
    db: AsyncSession, user: User, *, limit: int, offset: int
) -> list[Event]:
    return await repo.list_events_by_owner(db, user.id, limit=limit, offset=offset)


async def list_catalog_events(db: AsyncSession, *, limit: int, offset: int) -> list[Event]:
    return await repo.list_public_catalog(db, limit=limit, offset=offset)


async def _replace_images(
    db: AsyncSession, event_id: UUID, images: list[EventImageInput]
) -> None:
    await repo.delete_event_images(db, event_id)
    for image in images:
        await repo.create_event_image(
            db,
            event_id=event_id,
            object_key=image.object_key,
            alt_text=image.alt_text,
            sort_order=image.sort_order,
        )


_EVENT_EDITABLE_FIELDS = (
    "title",
    "description",
    "category",
    "visibility",
    "venue_name",
    "venue_address",
    "city",
    "country_code",
    "latitude",
    "longitude",
    "timezone",
    "starts_at",
    "ends_at",
    "registration_starts_at",
    "registration_ends_at",
    "capacity",
    "refund_policy",
)


async def update_event(
    db: AsyncSession, actor: User, event: Event, payload: EventUpdate
) -> Event:
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}

    for field in _EVENT_EDITABLE_FIELDS:
        if field in payload.model_fields_set:
            before[field] = audit_value(getattr(event, field))
            setattr(event, field, getattr(payload, field))
            after[field] = audit_value(getattr(event, field))

    if payload.model_fields_set & set(_EVENT_EDITABLE_FIELDS):
        _validate_event_time_order(event)
        await _validate_capacity_floor(db, event)

    if "images" in payload.model_fields_set and payload.images is not None:
        await _replace_images(db, event.id, payload.images)

    event.updated_at = _now()

    if before or after:
        await repo.add_audit_entry(
            db,
            actor_user_id=actor.id,
            event_id=event.id,
            entity_type="event",
            entity_id=event.id,
            action="event.updated",
            before_data=before or None,
            after_data=after or None,
        )
    logger.info("event %s updated by %s", event.id, actor.id)
    return event


def _validate_event_time_order(event: Event) -> None:
    ends = as_utc(event.ends_at)
    starts = as_utc(event.starts_at)
    if ends is not None and starts is not None and ends <= starts:
        raise ApiError(422, "EVENT_INVALID_TIME", "ends_at must be after starts_at")

    reg_start = as_utc(event.registration_starts_at)
    reg_end = as_utc(event.registration_ends_at)
    if reg_start is not None and reg_end is not None and reg_end <= reg_start:
        raise ApiError(
            422,
            "EVENT_INVALID_REGISTRATION",
            "registration_ends_at must be after registration_starts_at",
        )


async def _validate_capacity_floor(db: AsyncSession, event: Event) -> None:
    if event.capacity is None:
        return
    sold = await repo.count_event_sold_items(db, event.id)
    reserved = await repo.sum_event_active_holds(db, event.id, now=_now())
    if event.capacity < sold + reserved:
        raise ApiError(
            409,
            "CAPACITY_BELOW_SOLD",
            "Event capacity cannot be reduced below already sold and reserved quantities",
        )


async def delete_event(db: AsyncSession, actor: User, event: Event) -> None:
    if event.status != EventStatus.DRAFT:
        raise ApiError(
            409, "EVENT_NOT_DRAFT", "Only draft events can be deleted"
        )
    await repo.add_audit_entry(
        db,
        actor_user_id=actor.id,
        event_id=event.id,
        entity_type="event",
        entity_id=event.id,
        action="event.deleted",
        before_data={"title": event.title},
    )
    await db.delete(event)
    logger.info("draft event %s deleted by %s", event.id, actor.id)


def _transition_to(event: Event, target: EventStatus) -> None:
    if event.status not in _ALLOWED_TRANSITIONS or target not in _ALLOWED_TRANSITIONS[event.status]:
        raise ApiError(
            409,
            "INVALID_STATUS_TRANSITION",
            f"Cannot transition event from {event.status.value} to {target.value}",
        )


def collect_publish_errors(event: Event, ticket_types: list[TicketType]) -> list[str]:
    errors: list[str] = []
    if not event.title.strip():
        errors.append("title is required")
    if not (event.description and event.description.strip()):
        errors.append("description is required")
    if not (event.category and event.category.strip()):
        errors.append("category is required")
    if not (event.venue_name and event.venue_name.strip()):
        errors.append("venue_name is required")
    if as_utc(event.ends_at) <= as_utc(event.starts_at):  # type: ignore[operator]
        errors.append("ends_at must be after starts_at")
    if event.registration_starts_at is None or event.registration_ends_at is None:
        errors.append("registration window is required")
    elif as_utc(event.registration_ends_at) <= as_utc(event.registration_starts_at):  # type: ignore[operator]
        errors.append("registration_ends_at must be after registration_starts_at")
    if event.capacity is None or event.capacity <= 0:
        errors.append("capacity must be positive")

    visible_types = [t for t in ticket_types if not t.is_hidden]
    if not visible_types:
        errors.append("at least one visible ticket type is required")

    for ticket_type in ticket_types:
        if (
            ticket_type.sales_starts_at is not None
            and ticket_type.sales_ends_at is not None
            and as_utc(ticket_type.sales_ends_at) <= as_utc(ticket_type.sales_starts_at)  # type: ignore[operator]
        ):
            errors.append(
                f"ticket type '{ticket_type.name}' has an invalid sales window"
            )
    return errors


async def preview_event(
    db: AsyncSession, event: Event
) -> tuple[list[TicketType], list[str]]:
    ticket_types = await repo.list_ticket_types(db, event.id)
    errors = collect_publish_errors(event, ticket_types)
    return ticket_types, errors


async def publish_event(
    db: AsyncSession, actor: User, event: Event
) -> Event:
    ticket_types = await repo.list_ticket_types(db, event.id)
    errors = collect_publish_errors(event, ticket_types)
    if errors:
        raise ApiError(
            422,
            "EVENT_NOT_PUBLISHABLE",
            "Event is missing required data",
            details={"errors": errors},
        )

    _transition_to(event, EventStatus.PUBLISHED)

    if event.published_at is None:
        event.published_at = _now()
    event.status = EventStatus.PUBLISHED
    event.updated_at = _now()

    await repo.add_audit_entry(
        db,
        actor_user_id=actor.id,
        event_id=event.id,
        entity_type="event",
        entity_id=event.id,
        action="event.published",
        after_data={"status": event.status.value},
    )
    logger.info("event %s published by %s", event.id, actor.id)
    return event


async def unpublish_event(
    db: AsyncSession, actor: User, event: Event
) -> Event:
    _transition_to(event, EventStatus.UNPUBLISHED)
    event.status = EventStatus.UNPUBLISHED
    event.updated_at = _now()

    await repo.add_audit_entry(
        db,
        actor_user_id=actor.id,
        event_id=event.id,
        entity_type="event",
        entity_id=event.id,
        action="event.unpublished",
        after_data={"status": event.status.value},
    )
    logger.info("event %s unpublished by %s", event.id, actor.id)
    return event


async def cancel_event(db: AsyncSession, actor: User, event: Event) -> Event:
    _transition_to(event, EventStatus.CANCELLED)
    event.status = EventStatus.CANCELLED
    event.updated_at = _now()

    await repo.add_audit_entry(
        db,
        actor_user_id=actor.id,
        event_id=event.id,
        entity_type="event",
        entity_id=event.id,
        action="event.cancelled",
        after_data={"status": event.status.value},
    )
    logger.info("event %s cancelled by %s", event.id, actor.id)
    return event


async def duplicate_event(
    db: AsyncSession, actor: User, event: Event
) -> Event:
    copy = Event(
        owner_user_id=event.owner_user_id,
        title=event.title,
        description=event.description,
        category=event.category,
        visibility=event.visibility,
        status=EventStatus.DRAFT,
        venue_name=event.venue_name,
        venue_address=event.venue_address,
        city=event.city,
        country_code=event.country_code,
        latitude=event.latitude,
        longitude=event.longitude,
        timezone=event.timezone,
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        registration_starts_at=event.registration_starts_at,
        registration_ends_at=event.registration_ends_at,
        capacity=event.capacity,
        refund_policy=event.refund_policy,
        published_at=None,
    )
    db.add(copy)
    await db.flush()

    for image in await repo.list_event_images(db, event.id):
        await repo.create_event_image(
            db,
            event_id=copy.id,
            object_key=image.object_key,
            alt_text=image.alt_text,
            sort_order=image.sort_order,
        )

    for ticket_type in await repo.list_ticket_types(db, event.id):
        db.add(
            TicketType(
                event_id=copy.id,
                name=ticket_type.name,
                description=ticket_type.description,
                price=ticket_type.price,
                currency=ticket_type.currency,
                capacity=ticket_type.capacity,
                min_per_order=ticket_type.min_per_order,
                max_per_order=ticket_type.max_per_order,
                sales_starts_at=ticket_type.sales_starts_at,
                sales_ends_at=ticket_type.sales_ends_at,
                is_hidden=ticket_type.is_hidden,
            )
        )

    await repo.add_audit_entry(
        db,
        actor_user_id=actor.id,
        event_id=copy.id,
        entity_type="event",
        entity_id=copy.id,
        action="event.duplicated",
        after_data={"source_event_id": str(event.id)},
    )
    logger.info("event %s duplicated to %s by %s", event.id, copy.id, actor.id)
    return copy
