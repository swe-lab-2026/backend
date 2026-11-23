from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

SIMULATION_NOTICE = "DEMO ONLY — refunds are simulated; no real money is moved."


class RefundPolicyUpdate(BaseModel):
    refunds_enabled: bool
    # Customer refunds close this many hours before the event starts
    # (null/0 = until the start time).
    refund_deadline_hours: int | None = Field(default=None, ge=0, le=8760)
    # Optional human-readable policy shown to customers.
    policy_text: str | None = Field(default=None, max_length=10000)


class RefundPolicyResponse(BaseModel):
    event_id: UUID
    refunds_enabled: bool
    refund_deadline_hours: int | None
    refund_deadline: datetime | None
    policy_text: str | None


class RefundResponse(BaseModel):
    id: UUID
    order_id: UUID
    payment_id: UUID
    amount: Decimal
    currency: str
    status: str
    kind: str
    reason: str | None
    is_simulated: bool
    created_at: datetime
    completed_at: datetime | None


class RefundResultResponse(BaseModel):
    order_id: UUID
    order_status: str
    refund: RefundResponse | None
    # True when this request repeated an already-completed cancellation/refund:
    # nothing new was processed and the stored result is returned.
    replayed: bool
    notice: str = SIMULATION_NOTICE


class RefundEligibilityResponse(BaseModel):
    order_id: UUID
    cancellable: bool
    refundable: bool
    reason: str | None
    refund_amount: Decimal
    currency: str
    refunds_enabled: bool
    refund_deadline_hours: int | None
    refund_deadline: datetime | None
    policy_text: str | None


class EventCancelRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class EventCancellationSummary(BaseModel):
    event_id: UUID
    event_status: str
    cancelled_at: datetime | None
    reason: str | None
    orders_remaining: int
    orders_refunded: int
    orders_cancelled: int
    event_cancellation_refund_total: Decimal
    complete: bool


class AuditEntryResponse(BaseModel):
    id: int
    timestamp: datetime
    actor_id: UUID | None
    actor_type: str
    action: str
    entity_type: str
    entity_id: UUID | None
    event_id: UUID | None
    metadata: dict[str, Any] | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    request_id: str | None
