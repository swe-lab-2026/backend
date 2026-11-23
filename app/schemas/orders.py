from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.tickets import TicketResponse


class OrderItemRequest(BaseModel):
    ticket_type_id: UUID
    quantity: int = Field(ge=1, le=100)


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

    @model_validator(mode="after")
    def _single_promotion(self) -> "OrderCreateRequest":
        if self.promo_code is not None and self.campaign_token is not None:
            raise ValueError("Provide either promo_code or campaign_token, not both")
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
