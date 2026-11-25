import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db import repositories as repo
from app.db.models import (
    OrganizerProfile,
    PayoutAccount,
    PayoutStatus,
    User,
    VerificationStatus,
)
from app.schemas.organizers import (
    OrganizerProfileCreate,
    OrganizerProfileUpdate,
    PayoutAccountCreate,
)

logger = logging.getLogger("app.organizers")


def _strip(value: str | None) -> str | None:
    return value.strip() if isinstance(value, str) else value


def _normalize_contact_email(value: str | None) -> str | None:
    stripped = _strip(value)
    return stripped.lower() if stripped else None


async def create_organizer_profile(
    db: AsyncSession,
    user: User,
    payload: OrganizerProfileCreate,
) -> OrganizerProfile:
    existing = await repo.get_organizer_profile(db, user.id)
    if existing is not None:
        raise ApiError(
            409, "ORGANIZER_PROFILE_ALREADY_EXISTS", "Organizer profile already exists"
        )

    try:
        # A pre-flight SELECT cannot make the insert idempotent: two concurrent
        # requests can both observe "no profile". The primary key is the real
        # guard, so isolate the insert and translate the loser's IntegrityError
        # into the same 409 response as the sequential path.
        async with db.begin_nested():
            profile = await repo.create_organizer_profile(
                db,
                user_id=user.id,
                display_name=payload.display_name,
                legal_name=_strip(payload.legal_name),
                contact_email=_normalize_contact_email(payload.contact_email),
                contact_phone=_strip(payload.contact_phone),
                terms_accepted_at=(
                    datetime.now(timezone.utc) if payload.terms_accepted else None
                ),
            )
    except IntegrityError as exc:
        raise ApiError(
            409, "ORGANIZER_PROFILE_ALREADY_EXISTS", "Organizer profile already exists"
        ) from exc
    await repo.add_audit_entry(
        db,
        actor_user_id=user.id,
        entity_type="organizer_profile",
        entity_id=user.id,
        action="organizer.profile_created",
        after_data={"display_name": profile.display_name},
    )
    logger.info("organizer profile created for user %s", user.id)
    return profile


async def get_organizer_profile(db: AsyncSession, user: User) -> OrganizerProfile:
    profile = await repo.get_organizer_profile(db, user.id)
    if profile is None:
        raise ApiError(404, "ORGANIZER_PROFILE_NOT_FOUND", "Organizer profile not found")
    return profile


async def update_organizer_profile(
    db: AsyncSession,
    user: User,
    payload: OrganizerProfileUpdate,
) -> OrganizerProfile:
    profile = await get_organizer_profile(db, user)

    changes: dict[str, object] = {}
    if "display_name" in payload.model_fields_set:
        changes["display_name"] = payload.display_name
    if "legal_name" in payload.model_fields_set:
        changes["legal_name"] = _strip(payload.legal_name)
    if "contact_email" in payload.model_fields_set:
        changes["contact_email"] = _normalize_contact_email(payload.contact_email)
    if "contact_phone" in payload.model_fields_set:
        changes["contact_phone"] = _strip(payload.contact_phone)

    if changes:
        await repo.update_organizer_profile(db, profile, changes)
        await repo.add_audit_entry(
            db,
            actor_user_id=user.id,
            entity_type="organizer_profile",
            entity_id=user.id,
            action="organizer.profile_updated",
            after_data=changes,
        )
    return profile


def mask_external_account_ref(ref: str) -> str:
    """Mask a payout reference so sensitive account data is not exposed."""
    if len(ref) <= 4:
        return "****"
    return f"****{ref[-4:]}"


async def upsert_payout_account(
    db: AsyncSession,
    user: User,
    payload: PayoutAccountCreate,
) -> PayoutAccount:
    # 404 when the organizer has no profile yet: payout belongs to a profile.
    await get_organizer_profile(db, user)

    existing = await repo.get_payout_account_by_ref(
        db, provider=payload.provider, external_account_ref=payload.external_account_ref
    )
    if existing is not None and existing.organizer_user_id != user.id:
        raise ApiError(
            409,
            "PAYOUT_ACCOUNT_CONFLICT",
            "Payout account conflicts with an existing one",
        )

    try:
        async with db.begin_nested():
            # Only a request that asks for default may demote other accounts.
            # A non-default PUT must leave an unrelated current default alone.
            if payload.is_default and (
                existing is None or not existing.is_default
            ):
                await repo.clear_default_payout_account(db, user.id)
            if existing is None:
                account = await repo.create_payout_account(
                    db,
                    organizer_user_id=user.id,
                    provider=payload.provider,
                    external_account_ref=payload.external_account_ref,
                    is_default=payload.is_default,
                )
            else:
                await repo.update_payout_account(
                    db, existing, is_default=payload.is_default
                )
                account = existing
    except IntegrityError as exc:
        # Concurrent PUTs can race on the global (provider, reference) key or
        # on the single-default partial index.
        raise ApiError(
            409, "PAYOUT_ACCOUNT_CONFLICT", "Payout account conflicts with an existing one"
        ) from exc

    await repo.add_audit_entry(
        db,
        actor_user_id=user.id,
        entity_type="payout_account",
        entity_id=account.id,
        action="organizer.payout_account_updated",
        after_data={"provider": account.provider, "is_default": account.is_default},
    )
    return account


async def get_payout_account(db: AsyncSession, user: User) -> PayoutAccount:
    """The organizer's default payout account, else the most recent one."""
    await get_organizer_profile(db, user)
    account = await db.scalar(
        select(PayoutAccount)
        .where(PayoutAccount.organizer_user_id == user.id)
        .order_by(PayoutAccount.is_default.desc(), PayoutAccount.created_at.desc())
        .limit(1)
    )
    if account is None:
        raise ApiError(404, "PAYOUT_ACCOUNT_NOT_FOUND", "No payout account has been added")
    return account


async def set_verification_status(
    db: AsyncSession,
    actor: User,
    profile: OrganizerProfile,
    status: VerificationStatus,
    *,
    action: str,
    actor_type: str,
    reason: str | None = None,
) -> OrganizerProfile:
    previous = profile.verification_status
    profile.verification_status = status
    profile.updated_at = datetime.now(timezone.utc)
    await repo.add_audit_entry(
        db, actor_user_id=actor.id, actor_type=actor_type, entity_type="organizer_profile",
        entity_id=profile.user_id, action=action,
        before_data={"verification_status": previous.value},
        after_data={"verification_status": status.value},
        metadata={"reason": reason, "is_simulated": action.endswith("simulated")},
    )
    await db.flush()
    return profile


async def set_payout_status(
    db: AsyncSession,
    actor: User,
    account: PayoutAccount,
    status: PayoutStatus,
    *,
    action: str,
    actor_type: str,
    reason: str | None = None,
) -> PayoutAccount:
    previous = account.status
    account.status = status
    account.updated_at = datetime.now(timezone.utc)
    await repo.add_audit_entry(
        db, actor_user_id=actor.id, actor_type=actor_type, entity_type="payout_account",
        entity_id=account.id, action=action,
        before_data={"status": previous.value}, after_data={"status": status.value},
        metadata={"reason": reason, "is_simulated": action.endswith("simulated")},
    )
    await db.flush()
    return account


async def simulate_verification(db: AsyncSession, user: User) -> OrganizerProfile:
    profile = await get_organizer_profile(db, user)
    if profile.verification_status == VerificationStatus.VERIFIED:
        return profile
    return await set_verification_status(
        db, user, profile, VerificationStatus.VERIFIED,
        action="organizer.verification_simulated", actor_type="ORGANIZER",
    )


async def simulate_payout_approval(db: AsyncSession, user: User) -> PayoutAccount:
    account = await get_payout_account(db, user)
    if account.status == PayoutStatus.ACTIVE:
        return account
    return await set_payout_status(
        db, user, account, PayoutStatus.ACTIVE,
        action="organizer.payout_approval_simulated", actor_type="ORGANIZER",
    )
