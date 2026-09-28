from datetime import datetime, timezone
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AuditLog,
    EmailVerificationToken,
    RefreshSession,
    User,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- Users ----------

async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def get_user_by_id(session: AsyncSession, user_id: UUID) -> User | None:
    return await session.get(User, user_id)


async def create_user(
    session: AsyncSession,
    *,
    email: str,
    password_hash: str,
    first_name: str,
    last_name: str,
) -> User:
    user = User(
        email=email,
        password_hash=password_hash,
        first_name=first_name,
        last_name=last_name,
    )
    session.add(user)
    await session.flush()
    return user


# ---------- Email verification ----------

async def create_email_verification_token(
    session: AsyncSession,
    *,
    user_id: UUID,
    code_digest: str,
    expires_at: datetime,
) -> EmailVerificationToken:
    token = EmailVerificationToken(
        user_id=user_id,
        code_digest=code_digest,
        expires_at=expires_at,
    )
    session.add(token)
    await session.flush()
    return token


async def get_latest_email_verification_token(
    session: AsyncSession, user_id: UUID
) -> EmailVerificationToken | None:
    result = await session.execute(
        select(EmailVerificationToken)
        .where(EmailVerificationToken.user_id == user_id)
        .order_by(EmailVerificationToken.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def increment_verification_attempts(session: AsyncSession, token_id: UUID) -> None:
    await session.execute(
        update(EmailVerificationToken)
        .where(EmailVerificationToken.id == token_id)
        .values(attempt_count=EmailVerificationToken.attempt_count + 1)
    )


async def mark_verification_token_used(session: AsyncSession, token_id: UUID, at: datetime) -> None:
    await session.execute(
        update(EmailVerificationToken)
        .where(EmailVerificationToken.id == token_id)
        .values(used_at=at)
    )


# ---------- Refresh sessions ----------

async def create_refresh_session(
    session: AsyncSession,
    *,
    user_id: UUID,
    token_hash: str,
    jti: UUID,
    token_family: UUID,
    user_agent: str | None,
    ip_address: str | None,
    expires_at: datetime,
) -> RefreshSession:
    session_row = RefreshSession(
        user_id=user_id,
        token_hash=token_hash,
        jti=jti,
        token_family=token_family,
        user_agent=user_agent,
        ip_address=ip_address,
        expires_at=expires_at,
    )
    session.add(session_row)
    await session.flush()
    return session_row


async def get_refresh_session_by_jti(
    session: AsyncSession, jti: UUID, *, for_update: bool = False
) -> RefreshSession | None:
    stmt = select(RefreshSession).where(RefreshSession.jti == jti)
    if for_update:
        # Serializes concurrent refreshes of the same token so exactly one
        # succeeds; the loser observes the revoked row and triggers reuse
        # handling instead of issuing a second pair.
        stmt = stmt.with_for_update()
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def revoke_refresh_session(session: AsyncSession, session_id: UUID, at: datetime) -> None:
    await session.execute(
        update(RefreshSession)
        .where(RefreshSession.id == session_id)
        .values(revoked_at=at)
    )


async def revoke_refresh_family(session: AsyncSession, token_family: UUID, at: datetime) -> int:
    result = await session.execute(
        update(RefreshSession)
        .where(RefreshSession.token_family == token_family, RefreshSession.revoked_at.is_(None))
        .values(revoked_at=at)
    )
    return cast(CursorResult, result).rowcount or 0


async def revoke_all_user_sessions(session: AsyncSession, user_id: UUID, at: datetime) -> int:
    result = await session.execute(
        update(RefreshSession)
        .where(RefreshSession.user_id == user_id, RefreshSession.revoked_at.is_(None))
        .values(revoked_at=at)
    )
    return cast(CursorResult, result).rowcount or 0


async def touch_refresh_session(session: AsyncSession, session_id: UUID) -> None:
    await session.execute(
        update(RefreshSession)
        .where(RefreshSession.id == session_id)
        .values(last_used_at=_now())
    )


# ---------- Audit ----------

async def add_audit_entry(
    session: AsyncSession,
    *,
    actor_user_id: UUID | None,
    entity_type: str,
    entity_id: UUID | None,
    action: str,
    before_data: dict[str, Any] | None = None,
    after_data: dict[str, Any] | None = None,
    ip_address: str | None = None,
) -> None:
    entry = AuditLog(
        actor_user_id=actor_user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        before_data=before_data,
        after_data=after_data,
        ip_address=ip_address,
    )
    session.add(entry)