"""Platform Admin API schemas (SCRUM-17).

Responses never include password hashes, token digests, refresh sessions or
payment credentials (the platform stores none; payments are simulated).
"""

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class AdminReasonRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 3:
            raise ValueError("reason must contain at least 3 non-blank characters")
        return cleaned


class PersonRef(BaseModel):
    id: UUID
    name: str


class EventRef(BaseModel):
    id: UUID
    title: str


class AdminUserResponse(BaseModel):
    id: UUID
    user_id: UUID
    name: str
    email: str
    first_name: str
    last_name: str
    phone: str | None
    global_role: str
    status: str
    email_verified_at: datetime | None
    is_organizer: bool
    organizer_display_name: str | None
    organizer_verification_status: str | None
    suspended_at: datetime | None
    suspension_reason: str | None
    created_at: datetime
    updated_at: datetime


class AdminEventResponse(BaseModel):
    id: UUID
    title: str
    owner: PersonRef | None
    owner_user_id: UUID
    owner_email: str | None
    organizer_display_name: str | None
    status: str
    visibility: str
    starts_at: datetime
    ends_at: datetime
    city: str | None
    capacity: int | None
    is_suspended: bool
    suspended_at: datetime | None
    suspension_reason: str | None
    paid_sales_activated: bool
    paid_sales_suspended: bool
    published_at: datetime | None
    cancelled_at: datetime | None
    created_at: datetime


class AdminEventActionResponse(BaseModel):
    event: AdminEventResponse
    # Informational: existing orders/tickets are left in their lifecycle.
    pending_orders: int
    confirmed_orders: int


class AdminOrderResponse(BaseModel):
    id: UUID
    order_number: int
    event: EventRef | None
    event_id: UUID
    event_title: str | None
    buyer_user_id: UUID | None
    buyer_email: str
    status: str
    currency: str
    subtotal_amount: Decimal
    discount_amount: Decimal
    fee_amount: Decimal
    total_amount: Decimal
    campaign_id: UUID | None
    promo_code: str | None
    expires_at: datetime | None
    paid_at: datetime | None
    cancelled_at: datetime | None
    created_at: datetime


class AdminPaymentResponse(BaseModel):
    id: UUID
    order_id: UUID
    order_number: int | None
    event_id: UUID | None
    provider: str
    provider_payment_ref: str | None
    amount: Decimal
    currency: str
    status: str
    is_simulated: bool
    failure_code: str | None
    failure_message: str | None
    paid_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ActivationChecklist(BaseModel):
    verification_complete: bool
    payout_account_complete: bool
    activation_fee_paid: bool
    terms_accepted: bool


class AdminActivationResponse(BaseModel):
    event: EventRef | None
    organizer: PersonRef | None
    checklist: ActivationChecklist
    event_id: UUID
    event_title: str | None
    owner_user_id: UUID | None
    organizer_display_name: str | None
    organizer_verification_status: str | None
    # NOT_ACTIVATED | ACTIVE | SUSPENDED (derived from the activation row).
    status: str
    activation_fee_amount: Decimal
    activation_fee_paid_at: datetime | None
    terms_accepted_at: datetime | None
    activated_at: datetime | None
    sales_suspended_at: datetime | None
    sales_suspension_reason: str | None
    created_at: datetime
    updated_at: datetime


class AdminRefundResponse(BaseModel):
    id: UUID
    order_id: UUID
    order_number: int | None
    event: EventRef | None
    event_id: UUID | None
    payment_id: UUID
    amount: Decimal
    currency: str
    status: str
    kind: str
    reason: str | None
    is_simulated: bool
    initiated_by: UUID | None
    created_at: datetime
    completed_at: datetime | None


class DisputeStatusValue(str, Enum):
    OPEN = "OPEN"
    UNDER_REVIEW = "UNDER_REVIEW"
    RESOLVED = "RESOLVED"
    REJECTED = "REJECTED"


class DisputeCreate(BaseModel):
    payment_id: UUID
    reason: str = Field(min_length=3, max_length=2000)
    # Defaults to the full captured amount.
    amount: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    support_case_id: UUID | None = None


class DisputeUpdate(BaseModel):
    status: DisputeStatusValue
    resolution_note: str | None = Field(default=None, max_length=2000)


class DisputeResponse(BaseModel):
    id: UUID
    payment_id: UUID
    order_id: UUID
    event_id: UUID
    support_case_id: UUID | None
    status: str
    reason: str
    amount: Decimal
    currency: str
    resolution_note: str | None
    opened_by_user_id: UUID | None
    resolved_by_user_id: UUID | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class FailureKind(str, Enum):
    PAYMENT = "PAYMENT"
    REFUND = "REFUND"
    NOTIFICATION = "NOTIFICATION"


class OperationalFailureResponse(BaseModel):
    kind: str
    id: UUID
    order_id: UUID | None
    event_id: UUID | None
    code: str | None
    message: str | None
    attempts: int | None
    occurred_at: datetime


class AdminActionResponse(BaseModel):
    id: int
    timestamp: datetime
    admin_user_id: UUID | None
    admin_display_name: str | None
    action: str
    entity_type: str
    entity_id: UUID | None
    event_id: UUID | None
    reason: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None


class OrganizerVerificationUpdate(BaseModel):
    status: Literal["PENDING", "VERIFIED", "REJECTED"]
    reason: str | None = Field(default=None, max_length=1000)


class PayoutStatusUpdate(BaseModel):
    status: Literal["PENDING", "ACTIVE", "BLOCKED"]
    reason: str | None = Field(default=None, max_length=1000)


class AdminCampaignResponse(BaseModel):
    id: UUID
    event: EventRef | None
    name: str
    code: str | None
    status: str
    effective_status: str
    discount_type: str
    discount_value: Decimal
    redemption_count: int
    redemption_limit: int | None
    starts_at: datetime | None
    ends_at: datetime | None
    created_at: datetime


# ---------- Reported events (SRS 4.12) ----------


class EventReportReasonValue(str, Enum):
    FRAUD = "FRAUD"
    MISLEADING = "MISLEADING"
    INAPPROPRIATE = "INAPPROPRIATE"
    PROHIBITED = "PROHIBITED"
    SPAM = "SPAM"
    OTHER = "OTHER"


class EventReportStatusValue(str, Enum):
    OPEN = "OPEN"
    UNDER_REVIEW = "UNDER_REVIEW"
    RESOLVED = "RESOLVED"
    DISMISSED = "DISMISSED"


class EventReportCreate(BaseModel):
    reason: EventReportReasonValue
    details: str | None = Field(default=None, max_length=2000)

    @field_validator("details")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @model_validator(mode="after")
    def _other_needs_details(self) -> "EventReportCreate":
        if self.reason == EventReportReasonValue.OTHER and not self.details:
            raise ValueError("details are required when reason is OTHER")
        return self


class EventReportUpdate(BaseModel):
    status: EventReportStatusValue
    resolution_note: str | None = Field(default=None, max_length=2000)
    # Only with status RESOLVED: also suspend the event (separately audited).
    suspend_event: bool = False

    @model_validator(mode="after")
    def _suspend_needs_resolution(self) -> "EventReportUpdate":
        if self.suspend_event:
            if self.status != EventReportStatusValue.RESOLVED:
                raise ValueError("suspend_event is only allowed with status RESOLVED")
            if not self.resolution_note or len(self.resolution_note.strip()) < 3:
                raise ValueError("resolution_note (the suspension reason) is required")
        return self


class EventReportResponse(BaseModel):
    """What the reporter sees about their own report."""

    id: UUID
    event_id: UUID
    reason: str
    details: str | None
    status: str
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None


class AdminEventReportResponse(EventReportResponse):
    event: EventRef
    event_is_suspended: bool
    # Reports on the same event that are still OPEN or UNDER_REVIEW.
    event_active_reports: int
    reporter: PersonRef | None
    reporter_email: str | None
    resolution_note: str | None
    resolved_by_user_id: UUID | None


# ---------- Platform settings (SRS 4.12) ----------


class PlatformSettingsResponse(BaseModel):
    activation_fee_amount: Decimal
    activation_fee_currency: str
    processing_fee_percent: Decimal
    updated_at: datetime
    updated_by_user_id: UUID | None


class PlatformSettingsUpdate(BaseModel):
    activation_fee_amount: Decimal | None = Field(
        default=None, ge=0, le=Decimal("10000000"), max_digits=12, decimal_places=2
    )
    processing_fee_percent: Decimal | None = Field(
        default=None, ge=0, le=20, max_digits=5, decimal_places=2
    )
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _something_to_change(self) -> "PlatformSettingsUpdate":
        if self.activation_fee_amount is None and self.processing_fee_percent is None:
            raise ValueError("Provide activation_fee_amount and/or processing_fee_percent")
        return self
