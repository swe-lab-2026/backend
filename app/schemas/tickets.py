from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class TicketResponse(BaseModel):
    ticket_id: UUID
    public_id: UUID
    order_id: UUID
    order_number: int
    event_id: UUID
    event_title: str
    event_starts_at: datetime
    event_ends_at: datetime
    venue_name: str | None
    venue_address: str | None
    city: str | None
    timezone: str
    ticket_type_id: UUID
    ticket_type_name: str
    attendee_name: str | None
    attendee_email: str
    status: str
    issued_at: datetime
    checked_in_at: datetime | None
    cancelled_at: datetime | None
    qr_payload: str
    download_url: str


class TicketValidateRequest(BaseModel):
    qr_payload: str = Field(min_length=1, max_length=2048)


class TicketValidationResponse(BaseModel):
    valid: bool
    reason: str | None
    ticket: TicketResponse
