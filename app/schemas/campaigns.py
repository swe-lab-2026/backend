from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.orders import OrderItemRequest

PROMO_CODE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{2,39}$"

DiscountTypeLiteral = Literal["PERCENT", "FIXED"]


def _validate_discount(discount_type: str | None, value: Decimal | None) -> None:
    if value is None:
        return
    if value <= 0:
        raise ValueError("discount_value must be greater than 0")
    if value != value.quantize(Decimal("0.01")):
        raise ValueError("discount_value supports at most 2 decimal places")
    if discount_type == "PERCENT" and value > 100:
        raise ValueError("percentage discount must be at most 100")


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    code: str = Field(pattern=PROMO_CODE_PATTERN)
    discount_type: DiscountTypeLiteral
    # FIXED values are KZT amounts per order; PERCENT values are 0 < v <= 100.
    discount_value: Decimal = Field(max_digits=12, decimal_places=2)
    starts_at: datetime | None = None
    expires_at: datetime | None = None
    redemption_limit: int | None = Field(default=None, ge=1, le=1_000_000)
    # Empty list means the campaign applies to every ticket type of the event.
    ticket_type_ids: list[UUID] = Field(default_factory=list, max_length=100)
    is_active: bool = True

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value

    @model_validator(mode="after")
    def _check(self) -> "CampaignCreate":
        _validate_discount(self.discount_type, self.discount_value)
        if self.starts_at and self.expires_at and self.expires_at <= self.starts_at:
            raise ValueError("expires_at must be after starts_at")
        for field in ("starts_at", "expires_at"):
            value = getattr(self, field)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{field} must include a timezone offset")
        return self


class CampaignUpdate(BaseModel):
    """Partial update. Discount fields are locked once the code has been used."""

    name: str | None = Field(default=None, min_length=1, max_length=160)
    discount_type: DiscountTypeLiteral | None = None
    discount_value: Decimal | None = Field(default=None, max_digits=12, decimal_places=2)
    starts_at: datetime | None = None
    expires_at: datetime | None = None
    redemption_limit: int | None = Field(default=None, ge=1, le=1_000_000)
    ticket_type_ids: list[UUID] | None = Field(default=None, max_length=100)
    is_active: bool | None = None

    @model_validator(mode="after")
    def _check(self) -> "CampaignUpdate":
        _validate_discount(self.discount_type, self.discount_value)
        for field in ("starts_at", "expires_at"):
            value = getattr(self, field)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{field} must include a timezone offset")
        return self


class CampaignResponse(BaseModel):
    id: UUID
    event_id: UUID
    name: str
    code: str | None
    discount_type: str
    discount_value: Decimal
    starts_at: datetime | None
    expires_at: datetime | None
    redemption_limit: int | None
    redemption_count: int
    remaining_redemptions: int | None
    ticket_type_ids: list[UUID]
    is_active: bool
    # Derived from stored status + time window + capacity:
    # ACTIVE | SCHEDULED | EXPIRED | DISABLED | EXHAUSTED
    effective_status: str
    campaign_token: UUID
    qr_path: str
    qr_url: str | None
    created_at: datetime
    updated_at: datetime


class PromoValidateRequest(BaseModel):
    event_id: UUID
    code: str | None = Field(default=None, min_length=1, max_length=40)
    campaign_token: UUID | None = None
    items: list[OrderItemRequest] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def _one_source(self) -> "PromoValidateRequest":
        if (self.code is None) == (self.campaign_token is None):
            raise ValueError("Provide exactly one of code or campaign_token")
        return self


class PricedLineResponse(BaseModel):
    ticket_type_id: UUID
    quantity: int
    unit_price: Decimal
    subtotal: Decimal
    discount_amount: Decimal
    total: Decimal
    eligible: bool


class PromoQuoteResponse(BaseModel):
    """Server-calculated preview. Validating never consumes a redemption."""

    valid: bool = True
    campaign_id: UUID
    code: str | None
    discount_type: str
    discount_value: Decimal
    currency: str
    subtotal: Decimal
    eligible_subtotal: Decimal
    discount_amount: Decimal
    fee_amount: Decimal
    final_total: Decimal
    lines: list[PricedLineResponse]


class CampaignEventSummary(BaseModel):
    id: UUID
    title: str
    starts_at: datetime
    ends_at: datetime
    venue_name: str | None
    city: str | None
    timezone: str


class CampaignPromotion(BaseModel):
    campaign_token: UUID
    name: str
    discount_type: str
    discount_value: Decimal
    starts_at: datetime | None
    expires_at: datetime | None
    applies_to_all_ticket_types: bool
    ticket_type_ids: list[UUID]


class CampaignResolveResponse(BaseModel):
    """Public QR resolution. Exposes no counts, codes or buyer data."""

    event: CampaignEventSummary
    promotion: CampaignPromotion
    checkout: dict[str, str]


class CampaignAnalyticsResponse(BaseModel):
    campaign_id: UUID
    promo_code: str | None
    currency: str
    redemption_limit: int | None
    redemption_count: int
    reserved_redemptions: int
    successful_redemptions: int
    released_redemptions: int
    discount_total: Decimal
    attributed_order_count: int
    attributed_revenue: Decimal
    gross_attributed_order_count: int
    gross_attributed_revenue: Decimal
    reversed_order_count: int
    reversed_revenue: Decimal
