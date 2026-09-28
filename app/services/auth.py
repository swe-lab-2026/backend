import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ApiError
from app.core.security import (
    TOKEN_TYPE_REFRESH,
    constant_time_eq,
    create_access_token,
    create_refresh_token,
    decode_token,
    dummy_verify,
    hash_password,
    new_verification_code,
    sha256_hex,
    verify_password,
)
from app.db import repositories as repo
from app.db.models import User, UserStatus
from app.kv.store import TTLStore
from app.services.email import EmailProvider

logger = logging.getLogger("app.auth")


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(value: datetime) -> datetime:
    # SQLite stores TIMESTAMPTZ columns without timezone information and reads
    # them back naive; PostgreSQL reads them back aware. Normalize so expiry
    # comparisons behave the same on both dialects.
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _verification_expires_in(settings: Settings) -> int:
    return settings.email_code_ttl_minutes * 60


async def _create_and_send_code(
    db: AsyncSession,
    provider: EmailProvider,
    settings: Settings,
    *,
    user: User,
) -> int:
    code = new_verification_code()
    expires_at = _now() + timedelta(minutes=settings.email_code_ttl_minutes)
    await repo.create_email_verification_token(
        db,
        user_id=user.id,
        code_digest=sha256_hex(code),
        expires_at=expires_at,
    )
    await provider.send_verification_code(email=user.email, code=code)
    return _verification_expires_in(settings)


# ---------- Registration ----------

async def register(
    db: AsyncSession,
    provider: EmailProvider,
    settings: Settings,
    *,
    email: str,
    password: str,
    first_name: str,
    last_name: str,
) -> tuple[User, int]:
    email = normalize_email(email)
    existing = await repo.get_user_by_email(db, email)
    if existing is not None:
        raise ApiError(409, "EMAIL_ALREADY_EXISTS", "An account with this email already exists")

    user = await repo.create_user(
        db,
        email=email,
        password_hash=hash_password(password),
        first_name=first_name.strip(),
        last_name=last_name.strip(),
    )
    expires_in = await _create_and_send_code(db, provider, settings, user=user)
    logger.info("registered user %s (%s)", user.id, email)
    return user, expires_in


# ---------- Email verification ----------

async def verify_email(
    db: AsyncSession,
    settings: Settings,
    *,
    email: str,
    code: str,
) -> User:
    email = normalize_email(email)
    user = await repo.get_user_by_email(db, email)
    if user is None:
        # Same error as a bad code: an unregistered address is not disclosed.
        raise ApiError(400, "INVALID_VERIFICATION_CODE", "Invalid verification code")

    token = await repo.get_latest_email_verification_token(db, user.id)
    if token is None or token.used_at is not None:
        raise ApiError(400, "INVALID_VERIFICATION_CODE", "Invalid verification code")

    now = _now()
    if _ensure_utc(token.expires_at) < now:
        raise ApiError(400, "VERIFICATION_CODE_EXPIRED", "Verification code has expired")

    if token.attempt_count >= settings.email_code_max_attempts:
        raise ApiError(
            429, "TOO_MANY_ATTEMPTS", "Too many verification attempts. Request a new code."
        )

    if not constant_time_eq(token.code_digest, sha256_hex(code)):
        await repo.increment_verification_attempts(db, token.id)
        if token.attempt_count + 1 >= settings.email_code_max_attempts:
            # Burn the token so the client must request a fresh one.
            await repo.mark_verification_token_used(db, token.id, now)
        # Persist the attempt before the error response, which rolls the
        # surrounding transaction back.
        await db.commit()
        raise ApiError(400, "INVALID_VERIFICATION_CODE", "Invalid verification code")

    await repo.mark_verification_token_used(db, token.id, now)
    user.email_verified_at = now
    logger.info("verified email for user %s", user.id)
    return user


async def resend_verification(
    db: AsyncSession,
    provider: EmailProvider,
    store: TTLStore,
    settings: Settings,
    *,
    email: str,
) -> tuple[int, int]:
    email = normalize_email(email)
    user = await repo.get_user_by_email(db, email)
    if user is None:
        # Pretend the code was sent: an unknown address is not disclosed.
        return _verification_expires_in(settings), settings.email_code_resend_cooldown_seconds
    if user.email_verified_at is not None:
        raise ApiError(400, "EMAIL_ALREADY_VERIFIED", "Email is already verified")

    cooldown_key = f"resend:{email}"
    remaining = await store.ttl(cooldown_key)
    if remaining > 0:
        raise ApiError(429, "TOO_MANY_ATTEMPTS", f"Try again in {remaining} seconds")

    hour_key = f"resend:{email}:hour"
    count = await store.incr(hour_key, 3600)
    if count > settings.email_code_max_per_hour:
        raise ApiError(429, "TOO_MANY_ATTEMPTS", "Too many verification emails. Try again later.")

    await store.set(cooldown_key, "1", settings.email_code_resend_cooldown_seconds)
    expires_in = await _create_and_send_code(db, provider, settings, user=user)
    return expires_in, settings.email_code_resend_cooldown_seconds


# ---------- Login ----------

async def _enforce_login_rate_limit(
    store: TTLStore, settings: Settings, email: str, ip: str
) -> None:
    for key in (f"login:{email}", f"login:{ip}"):
        value = await store.get(key)
        if value is not None and int(value) >= settings.login_max_attempts:
            raise ApiError(
                429,
                "TOO_MANY_ATTEMPTS",
                "Too many failed login attempts. Try again later.",
            )


async def _record_login_failure(store: TTLStore, settings: Settings, email: str, ip: str) -> None:
    ttl = settings.login_lockout_minutes * 60
    for key in (f"login:{email}", f"login:{ip}"):
        await store.incr(key, ttl)


async def _clear_login_failures(store: TTLStore, email: str, ip: str) -> None:
    for key in (f"login:{email}", f"login:{ip}"):
        await store.delete(key)


async def issue_token_pair(
    db: AsyncSession,
    settings: Settings,
    user: User,
    *,
    user_agent: str | None,
    ip_address: str | None,
) -> TokenPair:
    family = uuid.uuid4()
    jti = uuid.uuid4()
    role = user.global_role.value
    refresh_token = create_refresh_token(
        settings, user_id=str(user.id), role=role, jti=str(jti)
    )
    expires_at = _now() + timedelta(days=settings.refresh_token_ttl_days)
    await repo.create_refresh_session(
        db,
        user_id=user.id,
        token_hash=sha256_hex(refresh_token),
        jti=jti,
        token_family=family,
        user_agent=user_agent,
        ip_address=ip_address,
        expires_at=expires_at,
    )
    access_token = create_access_token(settings, user_id=str(user.id), role=role)
    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.access_token_ttl_minutes * 60,
    )


async def login(
    db: AsyncSession,
    store: TTLStore,
    settings: Settings,
    *,
    email: str,
    password: str,
    user_agent: str | None,
    ip_address: str | None,
) -> tuple[User, TokenPair]:
    email = normalize_email(email)
    await _enforce_login_rate_limit(store, settings, email, ip_address or "unknown")

    user = await repo.get_user_by_email(db, email)
    if user is None:
        dummy_verify()  # same cost as a real bcrypt check
        await _record_login_failure(store, settings, email, ip_address or "unknown")
        raise ApiError(401, "INVALID_CREDENTIALS", "Invalid email or password")
    if not verify_password(password, user.password_hash):
        await _record_login_failure(store, settings, email, ip_address or "unknown")
        raise ApiError(401, "INVALID_CREDENTIALS", "Invalid email or password")

    await _clear_login_failures(store, email, ip_address or "unknown")

    if user.status == UserStatus.DELETED:
        raise ApiError(401, "INVALID_CREDENTIALS", "Invalid email or password")
    if user.email_verified_at is None:
        raise ApiError(403, "EMAIL_NOT_VERIFIED", "Email is not verified")
    if user.status == UserStatus.BLOCKED:
        await repo.add_audit_entry(
            db,
            actor_user_id=user.id,
            entity_type="user",
            entity_id=user.id,
            action="auth.login_blocked",
            ip_address=ip_address,
        )
        # Persist the audit entry before the error response rolls the
        # surrounding transaction back.
        await db.commit()
        raise ApiError(403, "USER_BLOCKED", "Account is blocked")

    pair = await issue_token_pair(
        db, settings, user, user_agent=user_agent, ip_address=ip_address
    )
    await repo.add_audit_entry(
        db,
        actor_user_id=user.id,
        entity_type="user",
        entity_id=user.id,
        action="auth.login",
        ip_address=ip_address,
    )
    return user, pair


# ---------- Refresh rotation ----------

async def rotate_refresh_token(
    db: AsyncSession,
    settings: Settings,
    raw_refresh_token: str,
    *,
    user_agent: str | None,
    ip_address: str | None,
) -> TokenPair:
    claims = decode_token(settings, raw_refresh_token, TOKEN_TYPE_REFRESH)
    now = _now()

    # The row lock serializes concurrent refreshes: exactly one rotation wins,
    # the loser sees revoked_at set and is handled as reuse below.
    session_row = await repo.get_refresh_session_by_jti(
        db, uuid.UUID(claims.jti), for_update=True
    )
    if session_row is None:
        raise ApiError(401, "TOKEN_INVALID", "Refresh session not found")

    if session_row.revoked_at is not None or not constant_time_eq(
        session_row.token_hash, sha256_hex(raw_refresh_token)
    ):
        # A revoked token was presented again — either a replay after rotation
        # or a forged token. Revoke the whole family and refuse.
        await repo.revoke_refresh_family(db, session_row.token_family, now)
        await repo.add_audit_entry(
            db,
            actor_user_id=session_row.user_id,
            entity_type="refresh_session",
            entity_id=session_row.id,
            action="auth.refresh_reuse",
            ip_address=ip_address,
        )
        # Persist the revocation before the error response, which rolls the
        # surrounding transaction back.
        await db.commit()
        raise ApiError(401, "REFRESH_TOKEN_REUSED", "Refresh token has already been used")

    if _ensure_utc(session_row.expires_at) < now:
        raise ApiError(401, "TOKEN_EXPIRED", "Refresh token has expired")

    user = await repo.get_user_by_id(db, session_row.user_id)
    if user is None or user.status == UserStatus.DELETED:
        raise ApiError(401, "TOKEN_INVALID", "User no longer exists")
    if user.status == UserStatus.BLOCKED:
        await repo.add_audit_entry(
            db,
            actor_user_id=user.id,
            entity_type="user",
            entity_id=user.id,
            action="auth.refresh_blocked",
            ip_address=ip_address,
        )
        # Persist the audit entry before the error response rolls the
        # surrounding transaction back.
        await db.commit()
        raise ApiError(403, "USER_BLOCKED", "Account is blocked")

    await repo.revoke_refresh_session(db, session_row.id, now)

    # Rotate inside the same family: a new token continues the device's chain.
    new_jti = uuid.uuid4()
    role = user.global_role.value
    refresh_token = create_refresh_token(
        settings, user_id=str(user.id), role=role, jti=str(new_jti)
    )
    expires_at = now + timedelta(days=settings.refresh_token_ttl_days)
    await repo.create_refresh_session(
        db,
        user_id=user.id,
        token_hash=sha256_hex(refresh_token),
        jti=new_jti,
        token_family=session_row.token_family,
        user_agent=user_agent,
        ip_address=ip_address,
        expires_at=expires_at,
    )
    access_token = create_access_token(settings, user_id=str(user.id), role=role)
    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.access_token_ttl_minutes * 60,
    )


# ---------- Logout ----------

async def logout(db: AsyncSession, settings: Settings, raw_refresh_token: str) -> None:
    try:
        claims = decode_token(settings, raw_refresh_token, TOKEN_TYPE_REFRESH)
    except ApiError:
        # Already invalid/expired: nothing to revoke server-side. Idempotent.
        return

    session_row = await repo.get_refresh_session_by_jti(
        db, uuid.UUID(claims.jti), for_update=True
    )
    if session_row is None or session_row.revoked_at is not None:
        return
    await repo.revoke_refresh_session(db, session_row.id, _now())
    await repo.add_audit_entry(
        db,
        actor_user_id=session_row.user_id,
        entity_type="refresh_session",
        entity_id=session_row.id,
        action="auth.logout",
    )


async def logout_all(db: AsyncSession, user_id: uuid.UUID) -> int:
    revoked = await repo.revoke_all_user_sessions(db, user_id, _now())
    await repo.add_audit_entry(
        db,
        actor_user_id=user_id,
        entity_type="user",
        entity_id=user_id,
        action="auth.logout_all",
    )
    return revoked