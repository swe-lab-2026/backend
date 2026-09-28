from collections.abc import AsyncIterator, Awaitable, Callable
from typing import NoReturn
from uuid import UUID

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import ApiError
from app.core.security import TOKEN_TYPE_ACCESS, decode_token
from app.db import repositories as repo
from app.db.models import Event, EventStaffRole, User, UserRole, UserStatus
from app.kv.store import TTLStore
from app.services.email import EmailProvider

bearer_scheme = HTTPBearer(auto_error=False)


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_store(request: Request) -> TTLStore:
    return request.app.state.store


def get_email_provider(request: Request) -> EmailProvider:
    return request.app.state.email_provider


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Authenticate the caller from a valid access token and load the live user.

    The user is re-fetched from PostgreSQL on every request so account
    blocking or a role change takes effect immediately, without waiting for
    the JWT to expire.
    """
    settings: Settings = request.app.state.settings
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise ApiError(401, "TOKEN_INVALID", "Bearer token is required")

    claims = decode_token(settings, credentials.credentials, TOKEN_TYPE_ACCESS)

    user = await repo.get_user_by_id(db, UUID(claims.user_id))
    if user is None or user.status == UserStatus.DELETED:
        raise ApiError(401, "TOKEN_INVALID", "User no longer exists")
    if user.status == UserStatus.BLOCKED:
        raise ApiError(403, "USER_BLOCKED", "Account is blocked")
    return user


async def require_active_user(user: User = Depends(get_current_user)) -> User:
    return user


async def require_verified_user(
    user: User = Depends(get_current_user),
) -> User:
    if user.email_verified_at is None:
        raise ApiError(403, "EMAIL_NOT_VERIFIED", "Email is not verified")
    return user


def require_roles(*roles: str) -> Callable[..., Awaitable[User]]:
    """Factory for role-gated dependencies, e.g. Depends(require_roles("PLATFORM_ADMIN"))."""

    async def checker(
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> User:
        if user.global_role.value not in roles:
            await repo.add_audit_entry(
                db,
                actor_user_id=user.id,
                entity_type="user",
                entity_id=user.id,
                action="auth.access_denied",
            )
            # Persist the audit entry before the error response rolls the
            # surrounding transaction back.
            await db.commit()
            raise ApiError(403, "INSUFFICIENT_PERMISSIONS", "Insufficient permissions")
        return user

    return checker


# ---------- Event-scoped authorization ----------

async def _deny_event_access(db: AsyncSession, user: User, event: Event) -> NoReturn:
    await repo.add_audit_entry(
        db,
        actor_user_id=user.id,
        event_id=event.id,
        entity_type="event",
        entity_id=event.id,
        action="event.staff_access_denied",
    )
    # Persist the audit entry before the error response rolls the surrounding
    # transaction back.
    await db.commit()
    raise ApiError(403, "INSUFFICIENT_EVENT_PERMISSIONS", "Insufficient permissions for this event")


async def require_event_owner(
    event_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Event:
    """Event owner or a PLATFORM_ADMIN. Used to manage a single event."""
    event = await repo.get_event_by_id(db, event_id)
    if event is None:
        raise ApiError(404, "EVENT_NOT_FOUND", "Event not found")
    if event.owner_user_id != user.id and user.global_role != UserRole.PLATFORM_ADMIN:
        await _deny_event_access(db, user, event)
    return event


def require_event_staff_role(*roles: EventStaffRole) -> Callable[..., Awaitable[Event]]:
    """Event-scoped dependency: only event staff with one of the given roles."""

    async def checker(
        event_id: UUID,
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> Event:
        event = await repo.get_event_by_id(db, event_id)
        if event is None:
            raise ApiError(404, "EVENT_NOT_FOUND", "Event not found")
        staff = await repo.get_event_staff(db, event_id=event_id, user_id=user.id)
        if staff is None or staff.role not in roles:
            await _deny_event_access(db, user, event)
        return event

    return checker


async def require_event_checkin_access(
    event_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Event:
    """Reusable gate for the future check-in API.

    Allows the event owner, a PLATFORM_ADMIN, or active event staff with
    EVENT_ADMIN / CHECK_IN_STAFF role. Every request re-queries PostgreSQL, so
    removing an EventStaff row revokes access immediately, even for an
    already-issued access token.
    """
    event = await repo.get_event_by_id(db, event_id)
    if event is None:
        raise ApiError(404, "EVENT_NOT_FOUND", "Event not found")
    if event.owner_user_id == user.id or user.global_role == UserRole.PLATFORM_ADMIN:
        return event
    staff = await repo.get_event_staff(db, event_id=event_id, user_id=user.id)
    if staff is not None and staff.role in (
        EventStaffRole.EVENT_ADMIN,
        EventStaffRole.CHECK_IN_STAFF,
    ):
        return event
    await _deny_event_access(db, user, event)