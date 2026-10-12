from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

from app.db.models import EventVisibility

_TWO_PLACES = Decimal("0.01")


def _strip_required(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be blank")
    return stripped


def _strip_optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _aware_or_raise(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        raise ValueError("must include a timezone offset")
    return value


def _quantize_price(value: Decimal) -> Decimal:
    try:
        return value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError("price must have at most 2 decimal places") from exc


def _validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("invalid IANA timezone") from exc
    return value


# ---------- Images ----------


class EventImageInput(BaseModel):
    object_key: str = Field(min_length=1, max_length=512)
    alt_text: str | None = Field(default=None, max_length=240)
    sort_order: int = Field(default=0, ge=0, le=32767)

    @field_validator("object_key")
    @classmethod
    def _object_key_required(cls, value: str) -> str:
        return _strip_required(value)


class EventImageResponse(BaseModel):
    id: UUID
    object_key: str
    alt_text: str | None
    sort_order: int


# ---------- Events ----------


class EventCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10000)
    category: str | None = Field(default=None, max_length=80)
    visibility: EventVisibility = EventVisibility.PUBLIC
    venue_name: str | None = Field(default=None, max_length=200)
    venue_address: str | None = Field(default=None, max_length=2000)
    city: str | None = Field(default=None, max_length=120)
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    latitude: Decimal | None = Field(default=None, ge=-90, le=90)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180)
    timezone: str = Field(min_length=1, max_length=64)
    starts_at: datetime
    ends_at: datetime
    registration_starts_at: datetime | None = None
    registration_ends_at: datetime | None = None
    capacity: int | None = Field(default=None, gt=0)
    refund_policy: str | None = Field(default=None, max_length=10000)
    images: list[EventImageInput] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def _title_required(cls, value: str) -> str:
        return _strip_required(value)

    @field_validator("description", "category", "venue_name", "venue_address", "city")
    @classmethod
    def _strip_optional_fields(cls, value: str | None) -> str | None:
        return _strip_optional(value)

    @field_validator("country_code")
    @classmethod
    def _country_code_upper(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.upper()

    @field_validator("timezone")
    @classmethod
    def _timezone_valid(cls, value: str) -> str:
        return _validate_timezone(value)

    @field_validator("starts_at", "ends_at", "registration_starts_at", "registration_ends_at")
    @classmethod
    def _datetimes_aware(cls, value: datetime | None) -> datetime | None:
        return _aware_or_raise(value)

    @model_validator(mode="after")
    def _validate_time_order(self) -> "EventCreate":
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        if (
            self.registration_starts_at is not None
            and self.registration_ends_at is not None
            and self.registration_ends_at <= self.registration_starts_at
        ):
            raise ValueError("registration_ends_at must be after registration_starts_at")
        return self


class EventUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10000)
    category: str | None = Field(default=None, max_length=80)
    visibility: EventVisibility | None = None
    venue_name: str | None = Field(default=None, max_length=200)
    venue_address: str | None = Field(default=None, max_length=2000)
    city: str | None = Field(default=None, max_length=120)
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    latitude: Decimal | None = Field(default=None, ge=-90, le=90)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    registration_starts_at: datetime | None = None
    registration_ends_at: datetime | None = None
    capacity: int | None = Field(default=None, gt=0)
    refund_policy: str | None = Field(default=None, max_length=10000)
    images: list[EventImageInput] | None = None

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _strip_required(value)

    @field_validator("description", "category", "venue_name", "venue_address", "city")
    @classmethod
    def _strip_optional_fields(cls, value: str | None) -> str | None:
        return _strip_optional(value)

    @field_validator("country_code")
    @classmethod
    def _country_code_upper(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.upper()

    @field_validator("timezone")
    @classmethod
    def _timezone_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_timezone(value)

    @field_validator(
        "starts_at", "ends_at", "registration_starts_at", "registration_ends_at"
    )
    @classmethod
    def _datetimes_aware(cls, value: datetime | None) -> datetime | None:
        return _aware_or_raise(value)


class EventResponse(BaseModel):
    id: UUID
    owner_user_id: UUID
    title: str
    description: str | None
    category: str | None
    visibility: str
    status: str
    venue_name: str | None
    venue_address: str | None
    city: str | None
    country_code: str | None
    latitude: Decimal | None
    longitude: Decimal | None
    timezone: str
    starts_at: datetime
    ends_at: datetime
    registration_starts_at: datetime | None
    registration_ends_at: datetime | None
    capacity: int | None
    refund_policy: str | None
    published_at: datetime | None
    images: list[EventImageResponse]
    created_at: datetime
    updated_at: datetime


class EventSummaryResponse(BaseModel):
    id: UUID
    title: str
    visibility: str
    status: str
    starts_at: datetime
    ends_at: datetime
    venue_name: str | None
    city: str | None
    timezone: str
    capacity: int | None
    created_at: datetime
    updated_at: datetime


# ---------- Ticket types ----------


class TicketTypeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    price: Decimal = Field(ge=0)
    currency: str = Field(default="KZT", min_length=3, max_length=3)
    capacity: int = Field(gt=0)
    min_per_order: int = Field(default=1, ge=1)
    max_per_order: int = Field(default=10, ge=1)
    sales_starts_at: datetime | None = None
    sales_ends_at: datetime | None = None
    is_hidden: bool = False

    @field_validator("name")
    @classmethod
    def _name_required(cls, value: str) -> str:
        return _strip_required(value)

    @field_validator("description")
    @classmethod
    def _description_strip(cls, value: str | None) -> str | None:
        return _strip_optional(value)

    @field_validator("currency")
    @classmethod
    def _currency_normalize(cls, value: str) -> str:
        normalized = value.strip().upper()
        if len(normalized) != 3 or not normalized.isalpha():
            raise ValueError("currency must be a 3-letter ISO code")
        return normalized

    @field_validator("price")
    @classmethod
    def _price_quantize(cls, value: Decimal) -> Decimal:
        return _quantize_price(value)

    @field_validator("sales_starts_at", "sales_ends_at")
    @classmethod
    def _sales_datetimes_aware(cls, value: datetime | None) -> datetime | None:
        return _aware_or_raise(value)

    @model_validator(mode="after")
    def _validate_order_limits(self) -> "TicketTypeCreate":
        if self.max_per_order < self.min_per_order:
            raise ValueError("max_per_order must be >= min_per_order")
        if (
            self.sales_starts_at is not None
            and self.sales_ends_at is not None
            and self.sales_ends_at <= self.sales_starts_at
        ):
            raise ValueError("sales_ends_at must be after sales_starts_at")
        return self


class TicketTypeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    price: Decimal | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    capacity: int | None = Field(default=None, gt=0)
    min_per_order: int | None = Field(default=None, ge=1)
    max_per_order: int | None = Field(default=None, ge=1)
    sales_starts_at: datetime | None = None
    sales_ends_at: datetime | None = None
    is_hidden: bool | None = None

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _strip_required(value)

    @field_validator("description")
    @classmethod
    def _description_strip(cls, value: str | None) -> str | None:
        return _strip_optional(value)

    @field_validator("currency")
    @classmethod
    def _currency_normalize(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if len(normalized) != 3 or not normalized.isalpha():
            raise ValueError("currency must be a 3-letter ISO code")
        return normalized

    @field_validator("price")
    @classmethod
    def _price_quantize(cls, value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        return _quantize_price(value)

    @field_validator("sales_starts_at", "sales_ends_at")
    @classmethod
    def _sales_datetimes_aware(cls, value: datetime | None) -> datetime | None:
        return _aware_or_raise(value)


class TicketTypeResponse(BaseModel):
    id: UUID
    event_id: UUID
    name: str
    description: str | None
    price: Decimal
    currency: str
    is_free: bool
    capacity: int
    min_per_order: int
    max_per_order: int
    sales_starts_at: datetime | None
    sales_ends_at: datetime | None
    is_hidden: bool
    created_at: datetime
    updated_at: datetime


# ---------- Inventory ----------


class TicketTypeInventory(BaseModel):
    ticket_type_id: UUID
    name: str
    is_hidden: bool
    capacity: int
    reserved: int
    sold: int
    returned: int
    checked_in: int
    available: int


class EventInventoryResponse(BaseModel):
    event_id: UUID
    event_capacity: int | None
    total_sold: int
    total_reserved: int
    total_available: int
    ticket_types: list[TicketTypeInventory]


# ---------- Reservation and purchase ----------


class HoldCreateRequest(BaseModel):
    quantity: int = Field(gt=0)
    expires_in_minutes: int = Field(default=15, ge=1, le=120)


class HoldResponse(BaseModel):
    hold_id: UUID
    event_id: UUID
    ticket_type_id: UUID
    quantity: int
    status: str
    expires_at: datetime
    created_at: datetime


class TicketItemResponse(BaseModel):
    ticket_id: UUID
    public_id: UUID
    status: str
    issued_at: datetime


class PurchaseRequest(BaseModel):
    quantity: int = Field(gt=0)


class PurchaseResponse(BaseModel):
    order_id: UUID
    order_number: int
    status: str
    currency: str
    total_amount: Decimal
    is_free: bool
    tickets: list[TicketItemResponse]
    hold: HoldResponse | None


class EventPreviewResponse(BaseModel):
    event: EventResponse
    ticket_types: list[TicketTypeResponse]
    publish_ready: bool
    validation_errors: list[str]
