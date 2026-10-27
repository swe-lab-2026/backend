from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.schemas.tickets import TicketResponse


class OrderItemCreate(BaseModel):
    ticket_type_id: UUID
    quantity: int = Field(gt=0, le=1000)


class OrderCreate(BaseModel):
    event_id: UUID
    items: list[OrderItemCreate] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _unique_ticket_types(self) -> "OrderCreate":
        seen: set[UUID] = set()
        for item in self.items:
            if item.ticket_type_id in seen:
                raise ValueError("each ticket type may appear only once per order")
            seen.add(item.ticket_type_id)
        return self


class OrderItemResponse(BaseModel):
    ticket_type_id: UUID
    ticket_type_name: str
    quantity: int
    unit_price: Decimal
    subtotal: Decimal


class OrderResponse(BaseModel):
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
