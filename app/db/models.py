import secrets
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum as PyEnum
from typing import Any

from sqlalchemy import (
    CHAR,
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, INET, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# audit_logs.id is BIGINT IDENTITY in the schema. The audit helper always
# supplies an explicit id so inserts work on both Postgres (BY DEFAULT AS
# IDENTITY) and the SQLite test fallback (which cannot auto-generate BIGINT
# identity). Kept below 2^63 so SQLite's signed INTEGER never overflows; the
# random seed prevents collisions across processes.
_audit_id_seed = secrets.randbits(48)
_audit_id_counter = 0


def _next_audit_id() -> int:
    global _audit_id_counter
    _audit_id_counter += 1
    return (_audit_id_seed + _audit_id_counter) % 2**63


# Cross-dialect types. Postgres runs the raw SQL file, so these variants only
# matter for the SQLite fallback used by the test suite.
CitextType = String().with_variant(CITEXT(), "postgresql")
JsonType = JSON().with_variant(JSONB(), "postgresql")
InetType = String().with_variant(INET(), "postgresql")
TimestampTz = DateTime(timezone=True)


# ---------- Enums ----------

class UserRole(PyEnum):
    USER = "USER"
    PLATFORM_ADMIN = "PLATFORM_ADMIN"


class UserStatus(PyEnum):
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"
    DELETED = "DELETED"


class VerificationStatus(PyEnum):
    NOT_SUBMITTED = "NOT_SUBMITTED"
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"


class PayoutStatus(PyEnum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"


class EventVisibility(PyEnum):
    PUBLIC = "PUBLIC"
    UNLISTED = "UNLISTED"
    PRIVATE = "PRIVATE"


class EventStatus(PyEnum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    UNPUBLISHED = "UNPUBLISHED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"


class EventStaffRole(PyEnum):
    EVENT_ADMIN = "EVENT_ADMIN"
    ORGANIZER_STAFF = "ORGANIZER_STAFF"
    CHECK_IN_STAFF = "CHECK_IN_STAFF"


class PaidSalesStatus(PyEnum):
    NOT_REQUESTED = "NOT_REQUESTED"
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"
    SUSPENDED = "SUSPENDED"


class OrderStatus(PyEnum):
    PENDING = "PENDING"
    PAID = "PAID"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    REFUNDED = "REFUNDED"


class HoldStatus(PyEnum):
    ACTIVE = "ACTIVE"
    CONSUMED = "CONSUMED"
    EXPIRED = "EXPIRED"
    RELEASED = "RELEASED"


class TicketStatus(PyEnum):
    VALID = "VALID"
    CHECKED_IN = "CHECKED_IN"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"


class PaymentStatus(PyEnum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"


class RefundStatus(PyEnum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class DiscountType(PyEnum):
    PERCENT = "PERCENT"
    FIXED = "FIXED"


class CampaignStatus(PyEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    EXPIRED = "EXPIRED"


class CheckinAction(PyEnum):
    CHECK_IN = "CHECK_IN"
    REVERSE = "REVERSE"


class SupportCaseStatus(PyEnum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class NotificationChannel(PyEnum):
    EMAIL = "EMAIL"
    PUSH = "PUSH"
    IN_APP = "IN_APP"


class NotificationStatus(PyEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    READ = "READ"


class EventSeatStatus(PyEnum):
    AVAILABLE = "AVAILABLE"
    HELD = "HELD"
    SOLD = "SOLD"
    BLOCKED = "BLOCKED"


# ---------- Identity and organizers ----------

class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(CitextType, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32))
    locale: Mapped[str] = mapped_column(String(10), nullable=False, default="en")
    global_role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role"), nullable=False, default=UserRole.USER
    )
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, name="user_status"), nullable=False, default=UserStatus.ACTIVE
    )
    email_verified_at: Mapped[datetime | None] = mapped_column(TimestampTz)
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)

    __table_args__ = (
        CheckConstraint("length(trim(email)) > 0", name="users_email_not_blank"),
    )


class RefreshSession(Base):
    """One row per issued refresh token. Only the token's SHA-256 digest is kept."""

    __tablename__ = "refresh_sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    jti: Mapped[uuid.UUID] = mapped_column(default=uuid.uuid4, nullable=False, unique=True)
    token_family: Mapped[uuid.UUID] = mapped_column(default=uuid.uuid4, nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(512))
    ip_address: Mapped[str | None] = mapped_column(String(45))
    expires_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(TimestampTz)
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(TimestampTz)

    __table_args__ = (
        Index("idx_refresh_sessions_user", "user_id"),
        Index(
            "idx_refresh_sessions_active",
            "user_id",
            "expires_at",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index("idx_refresh_sessions_family", "token_family"),
    )


class EmailVerificationToken(Base):
    """One row per 6-digit email code. Only the code's SHA-256 digest is kept."""

    __tablename__ = "email_verification_tokens"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    code_digest: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    used_at: Mapped[datetime | None] = mapped_column(TimestampTz)
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)

    __table_args__ = (Index("idx_email_verification_tokens_user", "user_id"),)


class OrganizerProfile(Base):
    __tablename__ = "organizer_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(200))
    contact_email: Mapped[str | None] = mapped_column(CitextType)
    contact_phone: Mapped[str | None] = mapped_column(String(32))
    verification_status: Mapped[VerificationStatus] = mapped_column(
        Enum(VerificationStatus, name="verification_status"),
        nullable=False,
        default=VerificationStatus.NOT_SUBMITTED,
    )
    terms_accepted_at: Mapped[datetime | None] = mapped_column(TimestampTz)
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)


class PayoutAccount(Base):
    __tablename__ = "payout_accounts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organizer_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizer_profiles.user_id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    external_account_ref: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[PayoutStatus] = mapped_column(
        Enum(PayoutStatus, name="payout_status"), nullable=False, default=PayoutStatus.PENDING
    )
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint(
            "provider", "external_account_ref", name="uq_payout_accounts_provider_ref"
        ),
        Index(
            "uq_payout_accounts_default",
            "organizer_user_id",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )


# ---------- Events and inventory ----------

class Event(Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizer_profiles.user_id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(80))
    visibility: Mapped[EventVisibility] = mapped_column(
        Enum(EventVisibility, name="event_visibility"),
        nullable=False,
        default=EventVisibility.PUBLIC,
    )
    status: Mapped[EventStatus] = mapped_column(
        Enum(EventStatus, name="event_status"), nullable=False, default=EventStatus.DRAFT
    )
    venue_name: Mapped[str | None] = mapped_column(String(200))
    venue_address: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(String(120))
    country_code: Mapped[str | None] = mapped_column(CHAR(2))
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False)
    ends_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False)
    registration_starts_at: Mapped[datetime | None] = mapped_column(TimestampTz)
    registration_ends_at: Mapped[datetime | None] = mapped_column(TimestampTz)
    capacity: Mapped[int | None] = mapped_column(Integer)
    refund_policy: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(TimestampTz)
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)

    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="events_time_order"),
        CheckConstraint("capacity IS NULL OR capacity > 0", name="events_capacity_positive"),
        CheckConstraint(
            "latitude IS NULL OR latitude BETWEEN -90 AND 90", name="events_latitude_range"
        ),
        CheckConstraint(
            "longitude IS NULL OR longitude BETWEEN -180 AND 180", name="events_longitude_range"
        ),
        CheckConstraint(
            "registration_starts_at IS NULL OR registration_ends_at IS NULL "
            "OR registration_ends_at > registration_starts_at",
            name="events_registration_order",
        ),
        Index(
            "idx_events_public_catalog",
            "starts_at",
            "status",
            postgresql_where=text("visibility = 'PUBLIC' AND status = 'PUBLISHED'"),
        ),
        Index("idx_events_owner", "owner_user_id", text("created_at DESC")),
    )


class EventImage(Base):
    __tablename__ = "event_images"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    alt_text: Mapped[str | None] = mapped_column(String(240))
    sort_order: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("event_id", "object_key", name="uq_event_images_event_object"),
    )


class EventStaff(Base):
    __tablename__ = "event_staff"

    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[EventStaffRole] = mapped_column(
        Enum(EventStaffRole, name="event_staff_role"), nullable=False
    )
    permissions: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)

    __table_args__ = (Index("idx_event_staff_user", "user_id"),)


class TicketType(Base):
    __tablename__ = "ticket_types"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0"))
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, default="KZT")
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    min_per_order: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_per_order: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    sales_starts_at: Mapped[datetime | None] = mapped_column(TimestampTz)
    sales_ends_at: Mapped[datetime | None] = mapped_column(TimestampTz)
    is_hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)

    __table_args__ = (
        CheckConstraint("price >= 0", name="ticket_types_price_nonnegative"),
        CheckConstraint("capacity > 0", name="ticket_types_capacity_positive"),
        CheckConstraint(
            "min_per_order > 0 AND max_per_order >= min_per_order", name="ticket_types_order_limits"
        ),
        CheckConstraint(
            "sales_starts_at IS NULL OR sales_ends_at IS NULL OR sales_ends_at > sales_starts_at",
            name="ticket_types_sales_order",
        ),
        UniqueConstraint("event_id", "name", name="uq_ticket_types_event_name"),
        Index("idx_ticket_types_event", "event_id"),
    )


class PaidSalesActivation(Base):
    __tablename__ = "paid_sales_activations"

    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    payout_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payout_accounts.id", ondelete="RESTRICT")
    )
    status: Mapped[PaidSalesStatus] = mapped_column(
        Enum(PaidSalesStatus, name="paid_sales_status"),
        nullable=False,
        default=PaidSalesStatus.NOT_REQUESTED,
    )
    activation_fee: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0")
    )
    terms_accepted_at: Mapped[datetime | None] = mapped_column(TimestampTz)
    activated_at: Mapped[datetime | None] = mapped_column(TimestampTz)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    review_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)

    __table_args__ = (
        CheckConstraint("activation_fee >= 0", name="paid_sales_fee_nonnegative"),
    )


# ---------- Promotions ----------

class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    promo_code: Mapped[str | None] = mapped_column(CitextType)
    campaign_token: Mapped[uuid.UUID] = mapped_column(
        default=uuid.uuid4, nullable=False, unique=True
    )
    discount_type: Mapped[DiscountType] = mapped_column(
        Enum(DiscountType, name="discount_type"), nullable=False
    )
    discount_value: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(TimestampTz)
    ends_at: Mapped[datetime | None] = mapped_column(TimestampTz)
    max_redemptions: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[CampaignStatus] = mapped_column(
        Enum(CampaignStatus, name="campaign_status"), nullable=False, default=CampaignStatus.DRAFT
    )
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)

    __table_args__ = (
        CheckConstraint("discount_value > 0", name="campaigns_discount_positive"),
        CheckConstraint(
            "discount_type <> 'PERCENT' OR discount_value <= 100", name="campaigns_percent_range"
        ),
        CheckConstraint(
            "max_redemptions IS NULL OR max_redemptions > 0", name="campaigns_redemptions_positive"
        ),
        CheckConstraint(
            "starts_at IS NULL OR ends_at IS NULL OR ends_at > starts_at",
            name="campaigns_time_order",
        ),
        UniqueConstraint("event_id", "promo_code", name="uq_campaigns_event_promo"),
        Index("idx_campaigns_event_status", "event_id", "status"),
    )


class CampaignTicketType(Base):
    __tablename__ = "campaign_ticket_types"

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True
    )
    ticket_type_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ticket_types.id", ondelete="CASCADE"), primary_key=True
    )


# ---------- Orders, attendees, and tickets ----------

class Order(Base):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    order_number: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False, unique=True)
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="RESTRICT"), nullable=False
    )
    buyer_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    buyer_email: Mapped[str] = mapped_column(CitextType, nullable=False)
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, name="order_status"), nullable=False, default=OrderStatus.PENDING
    )
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, default="KZT")
    subtotal_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0")
    )
    discount_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0")
    )
    fee_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0")
    )
    total_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0")
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime | None] = mapped_column(TimestampTz)
    paid_at: Mapped[datetime | None] = mapped_column(TimestampTz)
    cancelled_at: Mapped[datetime | None] = mapped_column(TimestampTz)
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)

    __table_args__ = (
        CheckConstraint(
            "subtotal_amount >= 0 AND discount_amount >= 0 AND fee_amount >= 0 "
            "AND total_amount >= 0",
            name="orders_amounts_nonnegative",
        ),
        CheckConstraint(
            "total_amount = subtotal_amount - discount_amount + fee_amount",
            name="orders_total_formula",
        ),
        Index("idx_orders_buyer", "buyer_user_id", text("created_at DESC")),
        Index("idx_orders_event_status", "event_id", "status", text("created_at DESC")),
    )


class Attendee(Base):
    __tablename__ = "attendees"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(CitextType, nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False
    )
    ticket_type_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ticket_types.id", ondelete="RESTRICT"), nullable=False
    )
    attendee_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("attendees.id", ondelete="SET NULL")
    )
    ticket_type_name: Mapped[str] = mapped_column(String(120), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0")
    )
    final_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    seat_label_snapshot: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)

    __table_args__ = (
        CheckConstraint(
            "unit_price >= 0 AND discount_amount >= 0 AND final_price >= 0",
            name="order_items_amounts_nonnegative",
        ),
        CheckConstraint(
            "final_price = unit_price - discount_amount", name="order_items_price_formula"
        ),
        Index("idx_order_items_order", "order_id"),
    )


class InventoryHold(Base):
    __tablename__ = "inventory_holds"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    ticket_type_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ticket_types.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[HoldStatus] = mapped_column(
        Enum(HoldStatus, name="hold_status"), nullable=False, default=HoldStatus.ACTIVE
    )
    expires_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)

    __table_args__ = (
        CheckConstraint("quantity > 0", name="inventory_holds_quantity_positive"),
        Index(
            "idx_inventory_holds_active",
            "ticket_type_id",
            "expires_at",
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    order_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("order_items.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    public_id: Mapped[uuid.UUID] = mapped_column(default=uuid.uuid4, nullable=False, unique=True)
    qr_token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    status: Mapped[TicketStatus] = mapped_column(
        Enum(TicketStatus, name="ticket_status"), nullable=False, default=TicketStatus.VALID
    )
    issued_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)
    checked_in_at: Mapped[datetime | None] = mapped_column(TimestampTz)
    cancelled_at: Mapped[datetime | None] = mapped_column(TimestampTz)
    updated_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_payment_ref: Mapped[str | None] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, name="payment_status"), nullable=False, default=PaymentStatus.PENDING
    )
    is_simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    failure_code: Mapped[str | None] = mapped_column(String(100))
    failure_message: Mapped[str | None] = mapped_column(Text)
    paid_at: Mapped[datetime | None] = mapped_column(TimestampTz)
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)

    __table_args__ = (
        CheckConstraint("amount > 0", name="payments_amount_positive"),
        UniqueConstraint("provider", "provider_payment_ref", name="uq_payments_provider_ref"),
        Index("idx_payments_order", "order_id"),
    )


class Refund(Base):
    __tablename__ = "refunds"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    payment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("payments.id", ondelete="RESTRICT"), nullable=False
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False
    )
    initiated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    provider_refund_ref: Mapped[str | None] = mapped_column(Text)
    status: Mapped[RefundStatus] = mapped_column(
        Enum(RefundStatus, name="refund_status"), nullable=False, default=RefundStatus.PENDING
    )
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)

    __table_args__ = (
        CheckConstraint("amount > 0", name="refunds_amount_positive"),
        Index("idx_refunds_order", "order_id"),
    )


class PromoRedemption(Base):
    __tablename__ = "promo_redemptions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="RESTRICT"), nullable=False
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    redeemed_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)

    __table_args__ = (
        CheckConstraint("discount_amount >= 0", name="promo_redemptions_nonnegative"),
    )


# ---------- Check-in, support, notifications, audit ----------

class CheckinRecord(Base):
    __tablename__ = "checkin_records"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tickets.id", ondelete="RESTRICT"), nullable=False
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="RESTRICT"), nullable=False
    )
    performed_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    action: Mapped[CheckinAction] = mapped_column(
        Enum(CheckinAction, name="checkin_action"), nullable=False
    )
    device_info: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)

    __table_args__ = (
        Index("idx_checkin_records_ticket", "ticket_id", text("created_at DESC")),
        Index("idx_checkin_records_event", "event_id", text("created_at DESC")),
    )


class SupportCase(Base):
    __tablename__ = "support_cases"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    requester_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    assignee_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL")
    )
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))
    ticket_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tickets.id", ondelete="SET NULL")
    )
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    subject: Mapped[str] = mapped_column(String(240), nullable=False)
    status: Mapped[SupportCaseStatus] = mapped_column(
        Enum(SupportCaseStatus, name="support_case_status"),
        nullable=False,
        default=SupportCaseStatus.OPEN,
    )
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(TimestampTz)

    __table_args__ = (
        Index("idx_support_cases_status", "status", text("created_at DESC")),
    )


class SupportMessage(Base):
    __tablename__ = "support_messages"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("support_cases.id", ondelete="CASCADE"), nullable=False
    )
    sender_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)

    __table_args__ = (
        CheckConstraint("length(trim(body)) > 0", name="support_messages_body_not_blank"),
    )


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(100), nullable=False)
    channel: Mapped[NotificationChannel] = mapped_column(
        Enum(NotificationChannel, name="notification_channel"), nullable=False
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    status: Mapped[NotificationStatus] = mapped_column(
        Enum(NotificationStatus, name="notification_status"),
        nullable=False,
        default=NotificationStatus.PENDING,
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(TimestampTz)
    sent_at: Mapped[datetime | None] = mapped_column(TimestampTz)
    read_at: Mapped[datetime | None] = mapped_column(TimestampTz)
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)

    __table_args__ = (
        Index(
            "idx_notifications_pending",
            "scheduled_at",
            "created_at",
            postgresql_where=text("status = 'PENDING'"),
        ),
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, default=_next_audit_id)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("events.id", ondelete="SET NULL"))
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column()
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    before_data: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    after_data: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    ip_address: Mapped[str | None] = mapped_column(InetType)
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)

    __table_args__ = (
        Index("idx_audit_logs_entity", "entity_type", "entity_id", text("created_at DESC")),
    )


# ---------- Optional module: assigned seating ----------

class Venue(Base):
    __tablename__ = "venues"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizer_profiles.user_id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    address: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(String(120))
    country_code: Mapped[str | None] = mapped_column(CHAR(2))
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)


class VenueLayout(Base):
    __tablename__ = "venue_layouts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    venue_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("venues.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("venue_id", "name", "version", name="uq_venue_layouts_venue_name_version"),
    )


class VenueSection(Base):
    __tablename__ = "venue_sections"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    layout_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("venue_layouts.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("layout_id", "name", name="uq_venue_sections_layout_name"),
    )


class VenueRow(Base):
    __tablename__ = "venue_rows"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    section_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("venue_sections.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(40), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("section_id", "label", name="uq_venue_rows_section_label"),
    )


class Seat(Base):
    __tablename__ = "seats"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    row_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("venue_rows.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(40), nullable=False)
    x_position: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    y_position: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))

    __table_args__ = (
        UniqueConstraint("row_id", "label", name="uq_seats_row_label"),
    )


class EventSeat(Base):
    __tablename__ = "event_seats"

    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    seat_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("seats.id", ondelete="RESTRICT"), primary_key=True
    )
    ticket_type_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ticket_types.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[EventSeatStatus] = mapped_column(
        Enum(EventSeatStatus, name="event_seat_status"),
        nullable=False,
        default=EventSeatStatus.AVAILABLE,
    )
    price_override: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    __table_args__ = (
        CheckConstraint(
            "price_override IS NULL OR price_override >= 0", name="event_seats_price_nonnegative"
        ),
    )


class SeatHold(Base):
    __tablename__ = "seat_holds"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    seat_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    status: Mapped[HoldStatus] = mapped_column(
        Enum(HoldStatus, name="hold_status"), nullable=False, default=HoldStatus.ACTIVE
    )
    expires_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False, default=_utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["event_id", "seat_id"],
            ["event_seats.event_id", "event_seats.seat_id"],
            ondelete="CASCADE",
        ),
        Index(
            "uq_seat_holds_active",
            "event_id",
            "seat_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )