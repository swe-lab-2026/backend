from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_app_settings,
    get_current_user,
    get_db,
    require_event_checkin_access,
)
from app.core.config import Settings
from app.db import repositories as repo
from app.db.models import Event, Ticket, User
from app.schemas.tickets import (
    TicketResponse,
    TicketValidateRequest,
    TicketValidationResponse,
)
from app.services import pdf_tickets
from app.services import tickets as ticket_service

router = APIRouter(prefix="/tickets", tags=["tickets"])
checkin_router = APIRouter(prefix="/events", tags=["check-in"])


async def _response(
    db: AsyncSession, ticket: Ticket, settings: Settings
) -> TicketResponse:
    return await ticket_service.build_ticket_response(
        db,
        ticket,
        qr_secret=settings.effective_ticket_qr_secret,
        download_base=settings.public_api_base,
    )


@router.get("", response_model=list[TicketResponse])
async def list_tickets(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> list[TicketResponse]:
    tickets = await repo.list_user_tickets(db, user.id, limit=limit, offset=offset)
    return [await _response(db, ticket, settings) for ticket in tickets]


@router.get("/{public_id}", response_model=TicketResponse)
async def get_ticket(
    public_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> TicketResponse:
    ticket, _order, _event, _ticket_type = await ticket_service.get_ticket_context(
        db, user, public_id
    )
    return await _response(db, ticket, settings)


@router.get("/{public_id}/pdf")
@router.get("/{public_id}/download")
async def download_ticket_pdf(
    public_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> Response:
    ticket, _order, _event, _ticket_type = await ticket_service.get_ticket_context(
        db, user, public_id
    )
    response = await _response(db, ticket, settings)
    pdf = pdf_tickets.generate_ticket_pdf(response)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="biletflow-ticket-{public_id}.pdf"'
        },
    )


@checkin_router.post(
    "/{event_id}/tickets/validate", response_model=TicketValidationResponse
)
async def validate_ticket(
    event_id: UUID,
    payload: TicketValidateRequest,
    event: Event = Depends(require_event_checkin_access),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> TicketValidationResponse:
    ticket, reason = await ticket_service.validate_ticket_qr(
        db,
        event=event,
        qr_payload=payload.qr_payload,
        qr_secret=settings.effective_ticket_qr_secret,
        download_base=settings.public_api_base,
    )
    return TicketValidationResponse(
        valid=reason is None,
        reason=reason,
        ticket=ticket,
    )


@checkin_router.post("/{event_id}/validate", response_model=TicketValidationResponse)
async def validate_ticket_alias(
    event_id: UUID,
    payload: TicketValidateRequest,
    event: Event = Depends(require_event_checkin_access),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> TicketValidationResponse:
    return await validate_ticket(event_id, payload, event, db, settings)


@checkin_router.post("/{event_id}/check-in", response_model=TicketResponse)
@checkin_router.post("/{event_id}/checkin", response_model=TicketResponse)
@checkin_router.post("/{event_id}/tickets/check-in", response_model=TicketResponse)
async def check_in(
    event_id: UUID,
    payload: TicketValidateRequest,
    event: Event = Depends(require_event_checkin_access),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> TicketResponse:
    return await ticket_service.check_in_ticket(
        db,
        event=event,
        qr_payload=payload.qr_payload,
        performed_by=user,
        qr_secret=settings.effective_ticket_qr_secret,
        download_base=settings.public_api_base,
    )
