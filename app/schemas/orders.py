from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class OrderItemRequest(BaseModel):
    ticket_type_id: UUID
    quantity: int = Field(ge=1, le=100)


class OrderCreateRequest(BaseModel):
    event_id: UUID
    items: list[OrderItemRequest] = Field(min_length=1, max_length=20)


class OrderItemResponse(BaseModel):
    ticket_type_id: UUID
    ticket_type_name: str
    quantity: int
    unit_price: Decimal
    final_price: Decimal


class OrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    event_id: UUID
    status: str
    currency: str
    subtotal_amount: Decimal
    total_amount: Decimal
    expires_at: datetime | None
    items: list[OrderItemResponse]


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
