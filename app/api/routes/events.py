from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_current_user,
    get_db,
    require_active_user,
    require_event_owner,
)
from app.db.models import Attendee, Event, Order, OrderItem, OrderStatus, User
from app.schemas.event_staff import (
    AssignedEventResponse,
    EventAdminAssignRequest,
    EventAdminResponse,
)
from app.services import event_staff as service

router = APIRouter(prefix="/events", tags=["events"])


def _event_response(event: Event) -> AssignedEventResponse:
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


@router.get("/assigned", response_model=list[AssignedEventResponse])
async def list_assigned_events(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(require_active_user),
    db: AsyncSession = Depends(get_db),
) -> list[AssignedEventResponse]:
    events = await service.list_assigned_events(db, user, limit=limit, offset=offset)
    return [_event_response(event) for event in events]


@router.get("/{event_id}/admins", response_model=list[EventAdminResponse])
async def list_event_admins(
    event: Event = Depends(require_event_owner),
    db: AsyncSession = Depends(get_db),
) -> list[EventAdminResponse]:
    return await service.list_event_admins(db, event)


@router.post("/{event_id}/admins", response_model=EventAdminResponse, status_code=201)
async def assign_event_admin(
    payload: EventAdminAssignRequest,
    event: Event = Depends(require_event_owner),
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EventAdminResponse:
    return await service.assign_event_admin(db, actor, event, email=payload.email)


@router.delete("/{event_id}/admins/{user_id}", status_code=204)
async def remove_event_admin(
    user_id: UUID,
    event: Event = Depends(require_event_owner),
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await service.remove_event_admin(db, actor, event, user_id=user_id)

@router.get("/{event_id}/attendees")
async def list_event_attendees(
    event: Event = Depends(require_event_owner),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """List attendees attached to confirmed (PAID, including free) orders."""
    stmt = (
        select(Attendee, OrderItem, Order)
        .join(OrderItem, OrderItem.attendee_id == Attendee.id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.event_id == event.id, Order.status == OrderStatus.PAID)
        .order_by(Attendee.created_at.desc())
    )
    rows = (await db.execute(stmt)).all()
    return [
        {
            "attendee_id": str(attendee.id),
            "full_name": attendee.full_name,
            "email": attendee.email,
            "phone": attendee.phone,
            "order_id": str(order.id),
            "ticket_type_id": str(item.ticket_type_id),
            "ticket_type_name": item.ticket_type_name,
        }
        for attendee, item, order in rows
    ]
