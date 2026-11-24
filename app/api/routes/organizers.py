from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_active_user, require_verified_user
from app.db.models import EventStatus, OrganizerProfile, PayoutAccount, User
from app.schemas.analytics import AnalyticsResponse, EventHistoryPage
from app.schemas.organizers import (
    OrganizerProfileCreate,
    OrganizerProfileResponse,
    OrganizerProfileUpdate,
    PayoutAccountCreate,
    PayoutAccountResponse,
)
from app.services import analytics as analytics_service
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


# ---------- Event history and aggregated analytics (SCRUM-17) ----------


@router.get("/me/events", response_model=EventHistoryPage)
async def list_event_history(
    category: list[Literal["UPCOMING", "ACTIVE", "COMPLETED", "CANCELLED"]] | None = Query(
        default=None
    ),
    status: list[EventStatus] | None = Query(default=None),
    q: str | None = Query(default=None, min_length=1, max_length=200),
    suspended: bool | None = None,
    starts_from: datetime | None = None,
    starts_to: datetime | None = None,
    sort: Literal["-starts_at", "starts_at", "-created_at", "created_at", "title", "-title"] = (
        "-starts_at"
    ),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(require_active_user),
    db: AsyncSession = Depends(get_db),
) -> EventHistoryPage:
    """Events the caller manages (owner, EVENT_ADMIN or ORGANIZER_STAFF).

    Category: CANCELLED (status), COMPLETED (status COMPLETED or ended),
    ACTIVE (started, not ended), UPCOMING (not started). Suspension is
    reported separately (``is_suspended``) and never changes the category.
    """
    return await analytics_service.event_history(
        db, user, categories=[str(c) for c in category] if category else None,
        statuses=status, q=q, suspended=suspended,
        starts_from=starts_from, starts_to=starts_to, sort=sort, limit=limit, offset=offset,
    )


@router.get("/me/analytics", response_model=AnalyticsResponse)
async def get_organizer_analytics(
    event_id: UUID | None = None,
    ticket_type_id: UUID | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    user: User = Depends(require_active_user),
    db: AsyncSession = Depends(get_db),
) -> AnalyticsResponse:
    """Analytics aggregated across every event the caller may analyze
    (owned, or staff with the ``view_analytics`` permission), optionally
    narrowed to one event and/or ticket type. Every reference is authorized
    before any aggregate is computed."""
    start, end = analytics_service.validate_range(start, end)
    event_ids, ticket_type = await analytics_service.resolve_scope(
        db,
        event_filter=await analytics_service.analytics_event_filter(db, user),
        event_id=event_id,
        ticket_type_id=ticket_type_id,
    )
    return await analytics_service.compute_analytics(
        db, event_ids=event_ids, ticket_type=ticket_type, start=start, end=end
    )
