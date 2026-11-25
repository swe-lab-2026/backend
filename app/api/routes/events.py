from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_app_settings,
    get_current_user,
    get_current_user_optional,
    get_db,
    get_email_provider,
    require_active_user,
    require_event_analytics_access,
    require_event_owner,
    require_verified_user,
)
from app.core.config import Settings
from app.core.errors import ApiError
from app.db import repositories as repo
from app.db.models import Event, EventImage, EventSalesActivation, TicketType, User
from app.schemas.admin import EventReportCreate, EventReportResponse
from app.schemas.analytics import AnalyticsResponse, TimelinePage
from app.schemas.event_staff import (
    AssignedEventResponse,
    EventAdminAssignRequest,
    EventAdminResponse,
)
from app.schemas.events import (
    EventCreate,
    EventImageResponse,
    EventImageUploadResponse,
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
from app.schemas.orders import EventAttendeeResponse
from app.schemas.paid_sales import (
    EventActivationResponse,
    TermsAcceptanceRequest,
)
from app.schemas.refunds import (
    AuditEntryResponse,
    EventCancellationSummary,
    EventCancelRequest,
    RefundPolicyResponse,
    RefundPolicyUpdate,
)
from app.services import analytics as analytics_service
from app.services import calendar as calendar_service
from app.services import event_staff as staff_service
from app.services import events as service
from app.services import inventory as inventory_service
from app.services import media as media_service
from app.services import notifications, paid_sales, refunds
from app.services import orders as order_service
from app.services import reports as report_service
from app.services import ticket_types as ticket_type_service
from app.services.email import EmailProvider

router = APIRouter(prefix="/events", tags=["events"])


def _assigned_response(event: Event, *, can_reverse: bool) -> AssignedEventResponse:
    return AssignedEventResponse(
        can_reverse_check_in=can_reverse,
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
        is_suspended=event.suspended_at is not None,
        suspended_at=event.suspended_at,
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
        is_suspended=event.suspended_at is not None,
    )


def _ticket_type_response(
    ticket_type: TicketType, available: int | None = None
) -> TicketTypeResponse:
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
        available=available,
    )


async def _event_detail(db: AsyncSession, event: Event) -> EventResponse:
    images = await repo.list_event_images(db, event.id)
    response = _event_detail_response(event, images)
    activation = await db.get(EventSalesActivation, event.id)
    response.paid_sales_active = (
        activation is not None
        and activation.activated_at is not None
        and activation.sales_suspended_at is None
        and event.suspended_at is None
    )
    return response


# ---------- Event staff (existing) ----------


@router.get("/assigned", response_model=list[AssignedEventResponse])
async def list_assigned_events(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(require_active_user),
    db: AsyncSession = Depends(get_db),
) -> list[AssignedEventResponse]:
    """Events where the authenticated user has permission to perform ticket
    verification and check-in, including events they own and events where they
    have an authorized staff assignment.
    """
    events = await staff_service.list_assigned_events(db, user, limit=limit, offset=offset)
    return [_assigned_response(event, can_reverse=flag) for event, flag in events]


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
    settings: Settings = Depends(get_app_settings),
    provider: EmailProvider = Depends(get_email_provider),
) -> EventResponse:
    await service.update_event(db, actor, event, payload)
    await _flush_outbox(db, settings, provider)
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


async def _flush_outbox(db: AsyncSession, settings: Settings, provider: EmailProvider) -> None:
    """Commit, then best-effort send queued emails (the worker retries the rest)."""
    await db.commit()
    await notifications.process_pending_notifications(
        db,
        provider=provider,
        download_base=settings.public_api_base,
        max_attempts=settings.notification_max_attempts,
        limit=200,
    )
    await db.commit()


@router.post("/{event_id}/cancel", response_model=EventResponse)
async def cancel_event(
    event_id: UUID,
    payload: EventCancelRequest | None = None,
    event: Event = Depends(require_event_owner),
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
    provider: EmailProvider = Depends(get_email_provider),
) -> EventResponse:
    """Cancel the event, invalidate tickets, refund paid orders, notify buyers.

    Orders are processed in committed batches; calling this again on an
    already-cancelled event resumes any unfinished work and never refunds or
    notifies twice. Progress: GET /events/{event_id}/cancellation.
    """
    event = await refunds.cancel_event(
        db, actor, event,
        reason=payload.reason if payload else None,
        batch_size=settings.event_cancellation_batch_size,
    )
    await _flush_outbox(db, settings, provider)
    return await _event_detail(db, event)


@router.get("/{event_id}/cancellation", response_model=EventCancellationSummary)
async def get_event_cancellation(
    event: Event = Depends(require_event_owner),
    db: AsyncSession = Depends(get_db),
) -> EventCancellationSummary:
    return EventCancellationSummary(**await refunds.cancellation_summary(db, event))


# ---------- Refund policy ----------


@router.get("/{event_id}/refund-policy", response_model=RefundPolicyResponse)
async def get_refund_policy(
    event_id: UUID,
    user: User | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
) -> RefundPolicyResponse:
    event = await service.get_visible_event(db, event_id, user)
    return RefundPolicyResponse(**refunds.policy_view(event))


@router.put("/{event_id}/refund-policy", response_model=RefundPolicyResponse)
async def update_refund_policy(
    payload: RefundPolicyUpdate,
    event: Event = Depends(require_event_owner),
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RefundPolicyResponse:
    await refunds.update_refund_policy(
        db, actor, event,
        refunds_enabled=payload.refunds_enabled,
        refund_deadline_hours=payload.refund_deadline_hours,
        policy_text=payload.policy_text,
        fields=set(payload.model_fields_set),
    )
    return RefundPolicyResponse(**refunds.policy_view(event))


# ---------- Audit log (read-only) ----------


@router.get("/{event_id}/audit-log", response_model=list[AuditEntryResponse])
async def list_event_audit_log(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    action: str | None = Query(default=None, max_length=100),
    action_prefix: str | None = Query(default=None, max_length=100),
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
    event: Event = Depends(require_event_owner),
    db: AsyncSession = Depends(get_db),
) -> list[AuditEntryResponse]:
    """Append-only trail for this event, newest first. There is deliberately no
    write route. ``from``/``to`` are inclusive dates in the event timezone;
    ``action_prefix`` matches e.g. ``refund.``."""
    rows = await analytics_service.event_audit_entries(
        db, event, action=action, action_prefix=action_prefix,
        date_from=date_from, date_to=date_to, limit=limit, offset=offset,
    )
    return [
        AuditEntryResponse(
            id=entry.id, timestamp=entry.created_at, actor_id=entry.actor_user_id,
            actor_type=entry.actor_type, action=entry.action,
            entity_type=entry.entity_type, entity_id=entry.entity_id,
            event_id=entry.event_id, metadata=entry.meta, before=entry.before_data,
            after=entry.after_data, request_id=entry.request_id,
            actor_name=f"{author.first_name} {author.last_name}".strip() if author else None,
            entity_label=label,
        )
        for entry, author, label in rows
    ]


# ---------- Paid-sales activation (per event) ----------


@router.get("/{event_id}/paid-sales/activation", response_model=EventActivationResponse)
async def get_paid_sales_activation(
    event: Event = Depends(require_event_owner),
    db: AsyncSession = Depends(get_db),
) -> EventActivationResponse:
    return EventActivationResponse.model_validate(
        await paid_sales.activation_status(db, event)
    )


@router.post("/{event_id}/paid-sales/terms", response_model=EventActivationResponse)
async def accept_paid_sales_terms(
    payload: TermsAcceptanceRequest,
    event: Event = Depends(require_event_owner),
    db: AsyncSession = Depends(get_db),
) -> EventActivationResponse:
    await paid_sales.accept_terms(db, event, payload.accepted)
    return EventActivationResponse.model_validate(
        await paid_sales.activation_status(db, event)
    )


@router.post("/{event_id}/paid-sales/activation-fee", response_model=EventActivationResponse)
async def simulate_activation_fee(
    event: Event = Depends(require_event_owner),
    db: AsyncSession = Depends(get_db),
) -> EventActivationResponse:
    # Explicitly an internal demo simulation; no real money is collected.
    await paid_sales.simulate_activation_fee(db, event)
    return EventActivationResponse.model_validate(
        await paid_sales.activation_status(db, event)
    )


@router.post("/{event_id}/paid-sales/activate", response_model=EventActivationResponse)
async def activate_paid_sales(
    event: Event = Depends(require_event_owner),
    db: AsyncSession = Depends(get_db),
) -> EventActivationResponse:
    return EventActivationResponse.model_validate(
        await paid_sales.activate_event(db, event)
    )


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
    available = await inventory_service.availability_by_type(db, event, ticket_types)
    return [_ticket_type_response(t, available[t.id]) for t in ticket_types]


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


# ---------- Confirmed attendees ----------


@router.get("/{event_id}/attendees", response_model=list[EventAttendeeResponse])
async def list_event_attendees(
    event_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    event: Event = Depends(require_event_owner),
    db: AsyncSession = Depends(get_db),
) -> list[EventAttendeeResponse]:
    """Return attendees only for confirmed (PAID) orders owned by this organizer."""
    rows = await order_service.list_event_attendees(
        db, event.id, limit=limit, offset=offset
    )
    return [EventAttendeeResponse(**row) for row in rows]


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


# ---------- Analytics and activity timeline (SCRUM-17) ----------


@router.get("/{event_id}/analytics", response_model=AnalyticsResponse)
async def get_event_analytics(
    start: datetime | None = None,
    end: datetime | None = None,
    ticket_type_id: UUID | None = None,
    event: Event = Depends(require_event_analytics_access),
    db: AsyncSession = Depends(get_db),
) -> AnalyticsResponse:
    """Capacity, sales, revenue, refunds, campaign and attendance metrics.

    ``start``/``end`` (UTC if no offset; ``start`` inclusive, ``end``
    exclusive) filter historical metrics only; capacity and attendance rate
    are current snapshots. ``sales_over_time`` buckets sales by day in the
    event timezone. See ``definitions`` in the response.
    """
    start, end = analytics_service.validate_range(start, end)
    event_ids, ticket_type = await analytics_service.resolve_scope(
        db,
        event_filter=Event.id == event.id,
        event_id=event.id,
        ticket_type_id=ticket_type_id,
    )
    return await analytics_service.compute_analytics(
        db, event_ids=event_ids, ticket_type=ticket_type, start=start, end=end,
        tz_name=event.timezone,
    )


@router.get("/{event_id}/timeline", response_model=TimelinePage)
async def get_event_timeline(
    action: list[str] | None = Query(default=None, max_length=20),
    entity_type: list[str] | None = Query(default=None, max_length=20),
    start: datetime | None = None,
    end: datetime | None = None,
    order: Literal["desc", "asc"] = "desc",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    event: Event = Depends(require_event_owner),
    db: AsyncSession = Depends(get_db),
) -> TimelinePage:
    """Chronological activity built from the append-only audit log.

    ``action`` may repeat; a value ending in ``.`` or ``.*`` is a prefix
    filter (e.g. ``refund.``). Entries carry actor, timestamp, action, entity
    and a redacted human-readable description (no raw payloads).
    """
    return await analytics_service.event_timeline(
        db, event, actions=action, entity_types=entity_type, start=start, end=end,
        order=order, limit=limit, offset=offset,
    )


# ---------- Event images (upload) ----------

_UPLOAD_CHUNK = 64 * 1024


@router.post(
    "/{event_id}/images/upload", response_model=EventImageUploadResponse, status_code=201
)
async def upload_event_image(
    event_id: UUID,
    file: Annotated[UploadFile, File()],
    event: Event = Depends(require_event_owner),
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> EventImageUploadResponse:
    """Store a JPEG, PNG or WebP image (multipart field ``file``).

    The image is re-encoded (metadata removed). The returned ``object_key`` is
    its URL; send it in ``images`` on PATCH /events/{id} to attach it.
    """
    limit = settings.media_max_upload_bytes
    data = bytearray()
    while chunk := await file.read(_UPLOAD_CHUNK):
        data.extend(chunk)
        if len(data) > limit:
            raise ApiError(
                413, "UPLOAD_TOO_LARGE", f"Images must be at most {limit // (1024 * 1024)} MB"
            )
    stored = await media_service.save_event_image(settings.media_root, event.id, bytes(data))
    url = f"{settings.public_api_base}/media/{stored.key}"
    await repo.add_audit_entry(
        db, actor_user_id=actor.id, entity_type="event", entity_id=event.id, event_id=event.id,
        action="event.image_uploaded",
        after_data={"key": stored.key, "size_bytes": stored.size_bytes},
    )
    return EventImageUploadResponse(
        object_key=url, url=url, content_type=stored.content_type,
        size_bytes=stored.size_bytes, width=stored.width, height=stored.height,
    )


# ---------- Calendar export ----------


@router.get(
    "/{event_id}/calendar.ics",
    response_class=Response,
    responses={200: {"content": {"text/calendar": {}}}},
)
async def export_event_calendar(
    event_id: UUID,
    user: User | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> Response:
    """iCalendar (.ics) file for one event; same visibility as GET /events/{id}."""
    event = await service.get_visible_event(db, event_id, user)
    base = settings.campaign_link_base
    body = calendar_service.build_event_ics(
        event, url=f"{base}/catalog/{event.id}" if base else None
    )
    return Response(
        content=body,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="event-{event.id}.ics"'},
    )


# ---------- Reporting an event (SRS 4.12) ----------


@router.post("/{event_id}/reports", response_model=EventReportResponse, status_code=201)
async def report_event(
    event_id: UUID,
    payload: EventReportCreate,
    user: User = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
) -> EventReportResponse:
    """Report a public event to the platform. One open report per user and event."""
    return await report_service.create_report(
        db, user, event_id, reason=payload.reason.value, details=payload.details
    )
