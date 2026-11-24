from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field


class EventActivationResponse(BaseModel):
    event_id: UUID
    verification_complete: bool
    payout_account_complete: bool
    activation_fee_amount: Decimal
    activation_fee_paid: bool
    terms_accepted: bool
    is_activated: bool
    activated_at: datetime | None
    # SCRUM-17: platform controls. Paid checkout requires paid_sales_open.
    sales_suspended: bool = False
    sales_suspended_at: datetime | None = None
    sales_suspension_reason: str | None = None
    event_suspended: bool = False
    paid_sales_open: bool = False


class TermsAcceptanceRequest(BaseModel):
    accepted: bool


class PaymentSimulationStartResponse(BaseModel):
    payment_id: UUID
    order_id: UUID
    amount: Decimal
    currency: str
    status: str
    is_simulated: bool = True
    notice: str = "DEMO ONLY — this is an internal payment simulation, not a real transaction."


class PaymentSimulationResultRequest(BaseModel):
    outcome: str = Field(pattern="^(success|failed|abandoned)$")


class PaymentResponse(BaseModel):
    id: UUID
    order_id: UUID
    amount: Decimal
    currency: str
    status: str
    is_simulated: bool
    provider: str
    failure_code: str | None
    created_at: datetime
    paid_at: datetime | None
    notice: str = "DEMO ONLY — this is an internal payment simulation, not a real transaction."
