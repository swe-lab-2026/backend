import logging
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db import repositories as repo
from app.db.models import OrganizerProfile, PayoutAccount, User
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

    profile = await repo.create_organizer_profile(
        db,
        user_id=user.id,
        display_name=payload.display_name,
        legal_name=_strip(payload.legal_name),
        contact_email=_normalize_contact_email(payload.contact_email),
        contact_phone=_strip(payload.contact_phone),
        terms_accepted_at=datetime.now(timezone.utc) if payload.terms_accepted else None,
    )
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

    await repo.clear_default_payout_account(db, user.id)
    try:
        account = await repo.create_payout_account(
            db,
            organizer_user_id=user.id,
            provider=payload.provider,
            external_account_ref=payload.external_account_ref,
            is_default=payload.is_default,
        )
    except IntegrityError as exc:
        # Two concurrent PUTs both claimed the single default slot; the
        # partial unique index rejected the second one.
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