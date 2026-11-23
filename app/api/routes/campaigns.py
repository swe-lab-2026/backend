import io
from uuid import UUID

import qrcode
from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_app_settings,
    get_current_user,
    get_db,
    require_active_user,
    require_event_owner,
)
from app.core.config import Settings
from app.core.errors import ApiError
from app.db.models import Event, User
from app.schemas.campaigns import (
    CampaignAnalyticsResponse,
    CampaignCreate,
    CampaignEventSummary,
    CampaignPromotion,
    CampaignResolveResponse,
    CampaignResponse,
    CampaignUpdate,
    PromoQuoteResponse,
    PromoValidateRequest,
)
from app.services import campaigns as service
from app.services import orders as order_service

# Organizer management, scoped to one event (owner or PLATFORM_ADMIN).
router = APIRouter(prefix="/events/{event_id}/campaigns", tags=["campaigns"])
# Customer-facing promotion endpoints.
public_router = APIRouter(prefix="/campaigns", tags=["campaigns"])


@router.post("", response_model=CampaignResponse, status_code=201)
async def create_campaign(
    payload: CampaignCreate,
    event: Event = Depends(require_event_owner),
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> CampaignResponse:
    campaign = await service.create_campaign(db, actor, event, payload)
    return await service.build_response(db, campaign, link_base=settings.campaign_link_base)


@router.get("", response_model=list[CampaignResponse])
async def list_campaigns(
    event: Event = Depends(require_event_owner),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> list[CampaignResponse]:
    campaigns = await service.list_campaigns(db, event)
    return [
        await service.build_response(db, campaign, link_base=settings.campaign_link_base)
        for campaign in campaigns
    ]


# Declared before /{campaign_id} so the literal path is matched first.
@router.get("/analytics", response_model=list[CampaignAnalyticsResponse])
async def list_campaign_analytics(
    event: Event = Depends(require_event_owner),
    db: AsyncSession = Depends(get_db),
) -> list[CampaignAnalyticsResponse]:
    return [
        await service.campaign_analytics(db, campaign)
        for campaign in await service.list_campaigns(db, event)
    ]


@router.get("/{campaign_id}", response_model=CampaignResponse)
async def get_campaign(
    campaign_id: UUID,
    event: Event = Depends(require_event_owner),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> CampaignResponse:
    campaign = await service.get_campaign(db, event, campaign_id)
    return await service.build_response(db, campaign, link_base=settings.campaign_link_base)


@router.patch("/{campaign_id}", response_model=CampaignResponse)
async def update_campaign(
    campaign_id: UUID,
    payload: CampaignUpdate,
    event: Event = Depends(require_event_owner),
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> CampaignResponse:
    campaign = await service.update_campaign(db, actor, event, campaign_id, payload)
    return await service.build_response(db, campaign, link_base=settings.campaign_link_base)


@router.get("/{campaign_id}/analytics", response_model=CampaignAnalyticsResponse)
async def get_campaign_analytics(
    campaign_id: UUID,
    event: Event = Depends(require_event_owner),
    db: AsyncSession = Depends(get_db),
) -> CampaignAnalyticsResponse:
    campaign = await service.get_campaign(db, event, campaign_id)
    return await service.campaign_analytics(db, campaign)


@router.get("/{campaign_id}/qr.png", response_class=Response)
async def get_campaign_qr_png(
    campaign_id: UUID,
    event: Event = Depends(require_event_owner),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> Response:
    campaign = await service.get_campaign(db, event, campaign_id)
    url = service.qr_url(campaign, settings.campaign_link_base)
    if url is None:
        raise ApiError(
            409,
            "CAMPAIGN_LINK_NOT_CONFIGURED",
            "PUBLIC_WEB_BASE_URL must be configured to render campaign QR codes",
        )
    image = qrcode.make(url, error_correction=qrcode.constants.ERROR_CORRECT_M)
    buffer = io.BytesIO()
    image.save(buffer)
    return Response(
        content=buffer.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "private, no-store"},
    )


@public_router.post("/validate", response_model=PromoQuoteResponse)
async def validate_promo(
    payload: PromoValidateRequest,
    _user: User = Depends(require_active_user),
    db: AsyncSession = Depends(get_db),
) -> PromoQuoteResponse:
    """Server-side promo validation and price preview. Never consumes a redemption."""
    return await order_service.quote_promotion(db, payload)


@public_router.get("/qr/{campaign_token}", response_model=CampaignResolveResponse)
async def resolve_campaign_qr(
    campaign_token: str,
    db: AsyncSession = Depends(get_db),
) -> CampaignResolveResponse:
    """Public resolution of a campaign QR token. Read-only; never consumes capacity."""
    campaign, event = await service.resolve_token(db, campaign_token)
    ticket_type_ids = await service.applicable_ticket_type_ids(db, campaign.id)
    return CampaignResolveResponse(
        event=CampaignEventSummary(
            id=event.id,
            title=event.title,
            starts_at=event.starts_at,
            ends_at=event.ends_at,
            venue_name=event.venue_name,
            city=event.city,
            timezone=event.timezone,
        ),
        promotion=CampaignPromotion(
            campaign_token=campaign.campaign_token,
            name=campaign.name,
            discount_type=campaign.discount_type.value,
            discount_value=campaign.discount_value,
            starts_at=campaign.starts_at,
            expires_at=campaign.ends_at,
            applies_to_all_ticket_types=not ticket_type_ids,
            ticket_type_ids=ticket_type_ids,
        ),
        checkout={
            "event_id": str(event.id),
            "campaign_token": str(campaign.campaign_token),
        },
    )
