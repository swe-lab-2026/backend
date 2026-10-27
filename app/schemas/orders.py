from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.tickets import TicketResponse


class OrderItemRequest(BaseModel):
    ticket_type_id: UUID
    quantity: int = Field(ge=1, le=100)


class OrderCreateRequest(BaseModel):
    event_id: UUID
    items: list[OrderItemRequest] = Field(min_length=1, max_length=20)


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
