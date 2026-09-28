from datetime import datetime, timezone
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AuditLog,
    EmailVerificationToken,
    Event,
    EventStaff,
    EventStaffRole,
    OrganizerProfile,
    PayoutAccount,
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
    event_id: UUID | None = None,
) -> None:
    entry = AuditLog(
        actor_user_id=actor_user_id,
        event_id=event_id,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        before_data=before_data,
        after_data=after_data,
        ip_address=ip_address,
    )
    session.add(entry)


# ---------- Organizer profiles ----------

async def get_organizer_profile(session: AsyncSession, user_id: UUID) -> OrganizerProfile | None:
    return await session.get(OrganizerProfile, user_id)


async def create_organizer_profile(
    session: AsyncSession,
    *,
    user_id: UUID,
    display_name: str,
    legal_name: str | None,
    contact_email: str | None,
    contact_phone: str | None,
    terms_accepted_at: datetime | None,
) -> OrganizerProfile:
    profile = OrganizerProfile(
        user_id=user_id,
        display_name=display_name,
        legal_name=legal_name,
        contact_email=contact_email,
        contact_phone=contact_phone,
        terms_accepted_at=terms_accepted_at,
    )
    session.add(profile)
    await session.flush()
    return profile


async def update_organizer_profile(
    session: AsyncSession, profile: OrganizerProfile, changes: dict[str, Any]
) -> None:
    for field, value in changes.items():
        setattr(profile, field, value)
    profile.updated_at = _now()


# ---------- Payout accounts ----------

async def create_payout_account(
    session: AsyncSession,
    *,
    organizer_user_id: UUID,
    provider: str,
    external_account_ref: str,
    is_default: bool,
) -> PayoutAccount:
    account = PayoutAccount(
        organizer_user_id=organizer_user_id,
        provider=provider,
        external_account_ref=external_account_ref,
        is_default=is_default,
    )
    session.add(account)
    await session.flush()
    return account


async def clear_default_payout_account(session: AsyncSession, organizer_user_id: UUID) -> None:
    await session.execute(
        update(PayoutAccount)
        .where(
            PayoutAccount.organizer_user_id == organizer_user_id,
            PayoutAccount.is_default.is_(True),
        )
        .values(is_default=False)
    )


# ---------- Events ----------

async def get_event_by_id(session: AsyncSession, event_id: UUID) -> Event | None:
    return await session.get(Event, event_id)


# ---------- Event staff ----------

async def get_event_staff(
    session: AsyncSession, *, event_id: UUID, user_id: UUID
) -> EventStaff | None:
    result = await session.execute(
        select(EventStaff).where(
            EventStaff.event_id == event_id,
            EventStaff.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def create_event_staff(
    session: AsyncSession,
    *,
    event_id: UUID,
    user_id: UUID,
    role: EventStaffRole,
    permissions: dict[str, Any],
) -> EventStaff:
    staff = EventStaff(
        event_id=event_id,
        user_id=user_id,
        role=role,
        permissions=permissions,
    )
    session.add(staff)
    await session.flush()
    return staff


async def delete_event_staff(
    session: AsyncSession, *, event_id: UUID, user_id: UUID
) -> int:
    result = await session.execute(
        delete(EventStaff).where(
            EventStaff.event_id == event_id,
            EventStaff.user_id == user_id,
        )
    )
    return cast(CursorResult, result).rowcount or 0


async def list_event_staff(
    session: AsyncSession, event_id: UUID, role: EventStaffRole
) -> list[tuple[EventStaff, User]]:
    result = await session.execute(
        select(EventStaff, User)
        .join(User, User.id == EventStaff.user_id)
        .where(EventStaff.event_id == event_id, EventStaff.role == role)
        .order_by(EventStaff.created_at.asc())
    )
    return [(row[0], row[1]) for row in result.all()]


async def list_assigned_events(
    session: AsyncSession,
    user_id: UUID,
    *,
    role: EventStaffRole,
    limit: int,
    offset: int,
) -> list[Event]:
    result = await session.execute(
        select(Event)
        .join(EventStaff, EventStaff.event_id == Event.id)
        .where(EventStaff.user_id == user_id, EventStaff.role == role)
        .order_by(Event.starts_at.asc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())