import logging
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db import repositories as repo
from app.db.models import Event, EventStaffRole, User, UserStatus
from app.schemas.event_staff import EventAdminResponse
from app.services.auth import normalize_email

logger = logging.getLogger("app.event_staff")

# The only capabilities granted to an Event Admin. Never includes owner
# transfer, staff management, other events, or platform-level actions.
EVENT_ADMIN_PERMISSIONS = {"view_event": True, "check_in_tickets": True}


async def assign_event_admin(
    db: AsyncSession,
    actor: User,
    event: Event,
    *,
    email: str,
) -> EventAdminResponse:
    email = normalize_email(email)
    target = await repo.get_user_by_email(db, email)
    if target is None or target.status == UserStatus.DELETED:
        # A deleted account is indistinguishable from a missing one.
        raise ApiError(404, "TARGET_USER_NOT_FOUND", "Target user not found")
    if target.status == UserStatus.BLOCKED:
        raise ApiError(409, "TARGET_USER_INACTIVE", "Target user is blocked")
    if target.email_verified_at is None:
        raise ApiError(409, "TARGET_USER_INACTIVE", "Target user email is not verified")
    if target.id == event.owner_user_id:
        raise ApiError(
            409,
            "EVENT_OWNER_CANNOT_BE_ADMIN",
            "Event owner cannot be assigned as an Event Admin of their own event",
        )

    existing = await repo.get_event_staff(db, event_id=event.id, user_id=target.id)
    if existing is not None:
        raise ApiError(
            409, "EVENT_ADMIN_ALREADY_ASSIGNED", "User is already an Event Admin of this event"
        )

    try:
        staff = await repo.create_event_staff(
            db,
            event_id=event.id,
            user_id=target.id,
            role=EventStaffRole.EVENT_ADMIN,
            permissions=dict(EVENT_ADMIN_PERMISSIONS),
        )
    except IntegrityError as exc:
        # Race: a concurrent request created the same (event_id, user_id) row.
        raise ApiError(
            409, "EVENT_ADMIN_ALREADY_ASSIGNED", "User is already an Event Admin of this event"
        ) from exc

    await repo.add_audit_entry(
        db,
        actor_user_id=actor.id,
        event_id=event.id,
        entity_type="event_staff",
        entity_id=target.id,
        action="event.admin_assigned",
        after_data={"role": staff.role.value, "permissions": staff.permissions},
    )
    logger.info("user %s assigned as Event Admin of event %s by %s", target.id, event.id, actor.id)
    return EventAdminResponse(
        user_id=target.id,
        email=target.email,
        first_name=target.first_name,
        last_name=target.last_name,
        role=staff.role.value,
        permissions=staff.permissions,
        assigned_at=staff.created_at,
    )


async def list_event_admins(db: AsyncSession, event: Event) -> list[EventAdminResponse]:
    rows = await repo.list_event_staff(db, event.id, EventStaffRole.EVENT_ADMIN)
    return [
        EventAdminResponse(
            user_id=user.id,
            email=user.email,
            first_name=user.first_name,
            last_name=user.last_name,
            role=staff.role.value,
            permissions=staff.permissions,
            assigned_at=staff.created_at,
        )
        for staff, user in rows
    ]


async def remove_event_admin(
    db: AsyncSession,
    actor: User,
    event: Event,
    *,
    user_id: UUID,
) -> None:
    staff = await repo.get_event_staff(db, event_id=event.id, user_id=user_id)
    if staff is None or staff.role != EventStaffRole.EVENT_ADMIN:
        raise ApiError(404, "EVENT_ADMIN_NOT_FOUND", "Event Admin assignment not found")

    before_data = {"role": staff.role.value, "permissions": staff.permissions}
    await repo.delete_event_staff(db, event_id=event.id, user_id=user_id)
    await repo.add_audit_entry(
        db,
        actor_user_id=actor.id,
        event_id=event.id,
        entity_type="event_staff",
        entity_id=user_id,
        action="event.admin_removed",
        before_data=before_data,
    )
    logger.info("user %s removed as Event Admin of event %s by %s", user_id, event.id, actor.id)


async def list_assigned_events(
    db: AsyncSession,
    user: User,
    *,
    limit: int,
    offset: int,
) -> list[Event]:
    # The filter is applied in SQL via the events<->event_staff join, never in
    # Python, so an Event Admin can only ever see their own assignments.
    return await repo.list_assigned_events(
        db,
        user.id,
        role=EventStaffRole.EVENT_ADMIN,
        limit=limit,
        offset=offset,
    )