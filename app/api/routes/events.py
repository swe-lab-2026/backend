from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_app_settings,
    get_current_user,
    get_current_user_optional,
    get_db,
    get_email_provider,
    require_active_user,
    require_event_owner,
)
from app.core.config import Settings
from app.db import repositories as repo
from app.db.models import Event, EventImage, TicketType, User
from app.schemas.event_staff import (
    AssignedEventResponse,
    EventAdminAssignRequest,
    EventAdminResponse,
)
from app.schemas.events import (
    EventCreate,
    EventImageResponse,
    EventInventoryResponse,
    EventPreviewResponse,
    EventResponse,
    EventSummaryResponse,
    EventUpdate,
    HoldCreateRequest,
    HoldResponse,
    PurchaseRequest,
    PurchaseResponse,
    TicketTypeCreate,
    TicketTypeResponse,
    TicketTypeUpdate,
)
from app.services import event_staff as staff_service
from app.services import events as service
from app.services import inventory as inventory_service
from app.services import notifications
from app.services import ticket_types as ticket_type_service
from app.services.email import EmailProvider

router = APIRouter(prefix="/events", tags=["events"])


def _assigned_response(event: Event) -> AssignedEventResponse:
    return AssignedEventResponse(
        event_id=event.id,
        title=event.title,
        status=event.status.value,
        visibility=event.visibility.value,
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        venue_name=event.venue_name,
        city=event.city,
        timezone=event.timezone,
    )


def _image_response(image: EventImage) -> EventImageResponse:
    return EventImageResponse(
        id=image.id,
        object_key=image.object_key,
        alt_text=image.alt_text,
        sort_order=image.sort_order,
    )


def _event_detail_response(event: Event, images: list[EventImage]) -> EventResponse:
    return EventResponse(
        id=event.id,
        owner_user_id=event.owner_user_id,
        title=event.title,
        description=event.description,
        category=event.category,
        visibility=event.visibility.value,
        status=event.status.value,
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
        published_at=event.published_at,
        images=[_image_response(image) for image in images],
        created_at=event.created_at,
        updated_at=event.updated_at,
    )


def _event_summary_response(event: Event) -> EventSummaryResponse:
    return EventSummaryResponse(
        id=event.id,
        title=event.title,
        visibility=event.visibility.value,
        status=event.status.value,
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        venue_name=event.venue_name,
        city=event.city,
        timezone=event.timezone,
        capacity=event.capacity,
        created_at=event.created_at,
        updated_at=event.updated_at,
    )


def _ticket_type_response(ticket_type: TicketType) -> TicketTypeResponse:
    return TicketTypeResponse(
        id=ticket_type.id,
        event_id=ticket_type.event_id,
        name=ticket_type.name,
        description=ticket_type.description,
        price=ticket_type.price,
        currency=ticket_type.currency,
        is_free=ticket_type.price == 0,
        capacity=ticket_type.capacity,
        min_per_order=ticket_type.min_per_order,
        max_per_order=ticket_type.max_per_order,
        sales_starts_at=ticket_type.sales_starts_at,
        sales_ends_at=ticket_type.sales_ends_at,
        is_hidden=ticket_type.is_hidden,
        created_at=ticket_type.created_at,
        updated_at=ticket_type.updated_at,
    )


async def _event_detail(db: AsyncSession, event: Event) -> EventResponse:
    images = await repo.list_event_images(db, event.id)
    return _event_detail_response(event, images)


# ---------- Event staff (existing) ----------


@router.get("/assigned", response_model=list[AssignedEventResponse])
async def list_assigned_events(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(require_active_user),
    db: AsyncSession = Depends(get_db),
) -> list[AssignedEventResponse]:
    events = await staff_service.list_assigned_events(db, user, limit=limit, offset=offset)
    return [_assigned_response(event) for event in events]


@router.get("/{event_id}/admins", response_model=list[EventAdminResponse])
async def list_event_admins(
    event: Event = Depends(require_event_owner),
    db: AsyncSession = Depends(get_db),
) -> list[EventAdminResponse]:
    return await staff_service.list_event_admins(db, event)


@router.post("/{event_id}/admins", response_model=EventAdminResponse, status_code=201)
async def assign_event_admin(
    payload: EventAdminAssignRequest,
    event: Event = Depends(require_event_owner),
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EventAdminResponse:
    return await staff_service.assign_event_admin(db, actor, event, email=payload.email)


@router.delete("/{event_id}/admins/{user_id}", status_code=204)
async def remove_event_admin(
    user_id: UUID,
    event: Event = Depends(require_event_owner),
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await staff_service.remove_event_admin(db, actor, event, user_id=user_id)


# ---------- Event management ----------


@router.get("/catalog", response_model=list[EventSummaryResponse])
async def list_public_catalog(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[EventSummaryResponse]:
    events = await service.list_catalog_events(db, limit=limit, offset=offset)
    return [_event_summary_response(event) for event in events]


@router.post("", response_model=EventResponse, status_code=201)
async def create_event(
    payload: EventCreate,
    user: User = Depends(require_active_user),
    db: AsyncSession = Depends(get_db),
) -> EventResponse:
    event = await service.create_event(db, user, payload)
    return await _event_detail(db, event)


@router.get("", response_model=list[EventSummaryResponse])
async def list_own_events(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(require_active_user),
    db: AsyncSession = Depends(get_db),
) -> list[EventSummaryResponse]:
    events = await service.list_owner_events(db, user, limit=limit, offset=offset)
    return [_event_summary_response(event) for event in events]


@router.get("/{event_id}", response_model=EventResponse)
async def get_event(
    event_id: UUID,
    user: User | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
) -> EventResponse:
    event = await service.get_visible_event(db, event_id, user)
    return await _event_detail(db, event)


@router.patch("/{event_id}", response_model=EventResponse)
async def update_event(
    event_id: UUID,
    payload: EventUpdate,
    event: Event = Depends(require_event_owner),
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EventResponse:
    await service.update_event(db, actor, event, payload)
    return await _event_detail(db, event)


@router.delete("/{event_id}", status_code=204)
async def delete_event(
    event_id: UUID,
    event: Event = Depends(require_event_owner),
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await service.delete_event(db, actor, event)


@router.post("/{event_id}/duplicate", response_model=EventResponse, status_code=201)
async def duplicate_event(
    event_id: UUID,
    event: Event = Depends(require_event_owner),
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EventResponse:
    copy = await service.duplicate_event(db, actor, event)
    return await _event_detail(db, copy)


@router.get("/{event_id}/preview", response_model=EventPreviewResponse)
async def preview_event(
    event_id: UUID,
    event: Event = Depends(require_event_owner),
    db: AsyncSession = Depends(get_db),
) -> EventPreviewResponse:
    ticket_types, errors = await service.preview_event(db, event)
    return EventPreviewResponse(
        event=await _event_detail(db, event),
        ticket_types=[_ticket_type_response(t) for t in ticket_types],
        publish_ready=not errors,
        validation_errors=errors,
    )


@router.post("/{event_id}/publish", response_model=EventResponse)
async def publish_event(
    event_id: UUID,
    event: Event = Depends(require_event_owner),
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EventResponse:
    await service.publish_event(db, actor, event)
    return await _event_detail(db, event)


@router.post("/{event_id}/unpublish", response_model=EventResponse)
async def unpublish_event(
    event_id: UUID,
    event: Event = Depends(require_event_owner),
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EventResponse:
    await service.unpublish_event(db, actor, event)
    return await _event_detail(db, event)


@router.post("/{event_id}/cancel", response_model=EventResponse)
async def cancel_event(
    event_id: UUID,
    event: Event = Depends(require_event_owner),
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EventResponse:
    await service.cancel_event(db, actor, event)
    return await _event_detail(db, event)


# ---------- Ticket types ----------


@router.post("/{event_id}/ticket-types", response_model=TicketTypeResponse, status_code=201)
async def create_ticket_type(
    event_id: UUID,
    payload: TicketTypeCreate,
    event: Event = Depends(require_event_owner),
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TicketTypeResponse:
    ticket_type = await ticket_type_service.create_ticket_type(db, actor, event, payload)
    return _ticket_type_response(ticket_type)


@router.get("/{event_id}/ticket-types", response_model=list[TicketTypeResponse])
async def list_ticket_types(
    event_id: UUID,
    user: User | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
) -> list[TicketTypeResponse]:
    event = await service.get_visible_event(db, event_id, user)
    include_hidden = service.is_authorized_viewer(event, user)
    ticket_types = await repo.list_ticket_types(db, event.id, include_hidden=include_hidden)
    return [_ticket_type_response(t) for t in ticket_types]


@router.patch(
    "/{event_id}/ticket-types/{ticket_type_id}",
    response_model=TicketTypeResponse,
)
async def update_ticket_type(
    event_id: UUID,
    ticket_type_id: UUID,
    payload: TicketTypeUpdate,
    event: Event = Depends(require_event_owner),
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TicketTypeResponse:
    ticket_type = await ticket_type_service.get_ticket_type(db, event, ticket_type_id)
    await ticket_type_service.update_ticket_type(db, actor, event, ticket_type, payload)
    return _ticket_type_response(ticket_type)


@router.delete("/{event_id}/ticket-types/{ticket_type_id}", status_code=204)
async def hide_ticket_type(
    event_id: UUID,
    ticket_type_id: UUID,
    event: Event = Depends(require_event_owner),
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    ticket_type = await ticket_type_service.get_ticket_type(db, event, ticket_type_id)
    await ticket_type_service.hide_ticket_type(db, actor, event, ticket_type)


# ---------- Inventory ----------


@router.get("/{event_id}/inventory", response_model=EventInventoryResponse)
async def get_inventory(
    event_id: UUID,
    event: Event = Depends(require_event_owner),
    db: AsyncSession = Depends(get_db),
) -> EventInventoryResponse:
    return await inventory_service.get_event_inventory(db, event)


@router.post(
    "/{event_id}/ticket-types/{ticket_type_id}/holds",
    response_model=HoldResponse,
    status_code=201,
)
async def create_hold(
    event_id: UUID,
    ticket_type_id: UUID,
    payload: HoldCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HoldResponse:
    return await inventory_service.create_hold(
        db,
        user,
        event_id,
        ticket_type_id,
        quantity=payload.quantity,
        expires_in_minutes=payload.expires_in_minutes,
    )


@router.post(
    "/{event_id}/ticket-types/{ticket_type_id}/purchase",
    response_model=PurchaseResponse,
    status_code=201,
)
async def purchase_ticket_type(
    event_id: UUID,
    ticket_type_id: UUID,
    payload: PurchaseRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
    provider: EmailProvider = Depends(get_email_provider),
) -> PurchaseResponse:
    result = await inventory_service.purchase(
        db, user, event_id, ticket_type_id, payload, settings=settings
    )
    if result.status in ("PAID", "CONFIRMED") and result.tickets:
        await db.commit()
        await notifications.deliver_order_confirmation(
            db,
            order_id=result.order_id,
            provider=provider,
            download_base=settings.public_api_base,
        )
        await db.commit()
    return result
