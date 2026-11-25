from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.schemas.tickets import TicketResponse


class OrderItemRequest(BaseModel):
    ticket_type_id: UUID
    quantity: int = Field(ge=1, le=100)


class AttendeeRequest(BaseModel):
    """Who a ticket is for. One entry per ticket."""

    ticket_type_id: UUID
    full_name: str = Field(min_length=1, max_length=200)
    email: EmailStr

    @field_validator("full_name")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("full_name must not be blank")
        return value.strip()


class OrderCreateRequest(BaseModel):
    """Checkout request.

    Only ticket selections and an optional promotion are accepted; prices,
    discounts, fees and totals are always calculated by the server. Unknown
    fields (e.g. a client-supplied ``total_amount``) are ignored.
    """

    event_id: UUID
    items: list[OrderItemRequest] = Field(min_length=1, max_length=20)
    promo_code: str | None = Field(default=None, min_length=1, max_length=40)
    # Opaque token from a campaign QR link (alternative to typing a code).
    campaign_token: UUID | None = None
    # Optional per-ticket attendee details; when omitted every ticket is
    # issued to the buyer account (previous behaviour).
    attendees: list[AttendeeRequest] | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def _single_promotion(self) -> "OrderCreateRequest":
        if self.promo_code is not None and self.campaign_token is not None:
            raise ValueError("Provide either promo_code or campaign_token, not both")
        if self.attendees is not None:
            wanted: dict[UUID, int] = {}
            for item in self.items:
                wanted[item.ticket_type_id] = wanted.get(item.ticket_type_id, 0) + item.quantity
            given: dict[UUID, int] = {}
            for attendee in self.attendees:
                given[attendee.ticket_type_id] = given.get(attendee.ticket_type_id, 0) + 1
            if wanted != given:
                raise ValueError(
                    "attendees must contain exactly one entry per ticket of each ticket type"
                )
        return self


OrderItemCreate = OrderItemRequest
OrderCreate = OrderCreateRequest


class OrderItemResponse(BaseModel):
    ticket_type_id: UUID
    ticket_type_name: str
    quantity: int
    unit_price: Decimal
    subtotal: Decimal
    final_price: Decimal


class OrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    order_id: UUID
    order_number: int
    event_id: UUID
    buyer_user_id: UUID | None
    buyer_email: str
    status: str
    currency: str
    subtotal_amount: Decimal
    discount_amount: Decimal
    fee_amount: Decimal
    total_amount: Decimal
    is_free: bool
    campaign_id: UUID | None = None
    promo_code: str | None = None
    expires_at: datetime | None
    paid_at: datetime | None
    cancelled_at: datetime | None
    created_at: datetime
    updated_at: datetime
    items: list[OrderItemResponse]
    tickets: list[TicketResponse]


class EventAttendeeResponse(BaseModel):
    """A unique attendee attached to a confirmed order."""

    attendee_id: UUID
    user_id: UUID | None
    full_name: str
    email: str
    phone: str | None
    registered_at: datetime
    order_id: UUID
    order_number: int
    currency: str
    total_amount: Decimal
    ticket_count: int
