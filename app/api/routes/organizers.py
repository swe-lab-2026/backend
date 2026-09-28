from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_verified_user
from app.db.models import OrganizerProfile, PayoutAccount, User
from app.schemas.organizers import (
    OrganizerProfileCreate,
    OrganizerProfileResponse,
    OrganizerProfileUpdate,
    PayoutAccountCreate,
    PayoutAccountResponse,
)
from app.services import organizers as service

router = APIRouter(prefix="/organizers", tags=["organizers"])


def _profile_response(profile: OrganizerProfile) -> OrganizerProfileResponse:
    return OrganizerProfileResponse(
        user_id=profile.user_id,
        display_name=profile.display_name,
        legal_name=profile.legal_name,
        contact_email=profile.contact_email,
        contact_phone=profile.contact_phone,
        verification_status=profile.verification_status.value,
        terms_accepted_at=profile.terms_accepted_at,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


def _payout_response(account: PayoutAccount) -> PayoutAccountResponse:
    return PayoutAccountResponse(
        id=account.id,
        provider=account.provider,
        external_account_ref=service.mask_external_account_ref(account.external_account_ref),
        status=account.status.value,
        is_default=account.is_default,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


@router.post("/me", response_model=OrganizerProfileResponse, status_code=201)
async def create_organizer_profile(
    payload: OrganizerProfileCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_verified_user),
) -> OrganizerProfileResponse:
    profile = await service.create_organizer_profile(db, user, payload)
    return _profile_response(profile)


@router.get("/me", response_model=OrganizerProfileResponse)
async def get_organizer_profile(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_verified_user),
) -> OrganizerProfileResponse:
    profile = await service.get_organizer_profile(db, user)
    return _profile_response(profile)


@router.patch("/me", response_model=OrganizerProfileResponse)
async def update_organizer_profile(
    payload: OrganizerProfileUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_verified_user),
) -> OrganizerProfileResponse:
    profile = await service.update_organizer_profile(db, user, payload)
    return _profile_response(profile)


@router.put("/me/payout-account", response_model=PayoutAccountResponse)
async def upsert_payout_account(
    payload: PayoutAccountCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_verified_user),
) -> PayoutAccountResponse:
    account = await service.upsert_payout_account(db, user, payload)
    return _payout_response(account)