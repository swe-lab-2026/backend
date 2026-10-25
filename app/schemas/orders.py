from uuid import UUID
from pydantic import BaseModel, Field, ConfigDict
from decimal import Decimal
from datetime import datetime

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
