from collections.abc import AsyncIterator, Awaitable, Callable
from uuid import UUID

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import ApiError
from app.core.security import TOKEN_TYPE_ACCESS, decode_token
from app.db import repositories as repo
from app.db.models import User, UserStatus
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