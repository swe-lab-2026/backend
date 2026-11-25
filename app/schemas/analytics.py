from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel


class AnalyticsScope(BaseModel):
    event_ids: list[UUID]
    event_count: int
    ticket_type_id: UUID | None
    start: datetime | None
    end: datetime | None
    # Time zone used to bucket sales_over_time by day.
    timezone: str = "UTC"
    generated_at: datetime


class CapacitySnapshot(BaseModel):
    """Current state; never filtered by the date range."""

    total_capacity: int
    sold_tickets: int
    reserved_tickets: int
    remaining_tickets: int


class AttendanceMetrics(BaseModel):
    # Snapshot (current ticket statuses).
    checked_in: int
    eligible_tickets: int
    attendance_rate: float | None
    cancelled_tickets: int
    refunded_tickets: int
    # Period (check-in time within the date range).
    period_check_ins: int
    period_reversals: int
    period_admitted_tickets: int


class CurrencySales(BaseModel):
    """Realized sales in one currency for the period. Never mixes currencies."""

    currency: str
    orders: int
    tickets_sold: int
    tickets_reversed: int
    gross_ticket_sales: Decimal
    discounts: Decimal
    ticket_revenue: Decimal
    fees: Decimal | None
    gross_revenue: Decimal
    refund_count: int
    refunds: Decimal
    net_revenue: Decimal
    # Demo payout estimate; see ``definitions["estimated_payout"]``.
    refunded_ticket_revenue: Decimal = Decimal("0")
    refunded_fees: Decimal | None = None
    estimated_payout: Decimal = Decimal("0")


class TicketTypeMetrics(BaseModel):
    ticket_type_id: UUID
    event_id: UUID
    name: str
    currency: str
    price: Decimal
    is_hidden: bool
    # Snapshot.
    capacity: int
    sold_tickets: int
    reserved_tickets: int
    remaining_tickets: int
    checked_in: int
    # Period (payment confirmation time).
    period_tickets_sold: int
    period_tickets_reversed: int
    period_gross_sales: Decimal
    period_discounts: Decimal
    period_revenue: Decimal


class CampaignSales(BaseModel):
    currency: str
    orders: int
    tickets_sold: int
    reversed_orders: int
    discounts: Decimal
    revenue: Decimal


class CampaignMetrics(BaseModel):
    campaign_id: UUID
    event_id: UUID
    name: str
    promo_code: str | None
    status: str
    # Snapshot of promo_redemptions by lifecycle state.
    reserved_redemptions: int
    successful_redemptions: int
    released_redemptions: int
    # Period, by payment confirmation time, per currency.
    sales: list[CampaignSales]


class SalesOverTimePoint(BaseModel):
    """One day of realized sales (orders.paid_at in ``scope.timezone``)."""

    date: date
    currency: str
    orders: int
    tickets: int
    gross: Decimal  # list prices, no fees


class AnalyticsResponse(BaseModel):
    scope: AnalyticsScope
    definitions: dict[str, str]
    capacity: CapacitySnapshot
    attendance: AttendanceMetrics
    sales: list[CurrencySales]
    # Daily breakdown of ``sales`` for charts (added after PR 19; additive).
    sales_over_time: list[SalesOverTimePoint] = []
    ticket_types: list[TicketTypeMetrics]
    campaigns: list[CampaignMetrics]


class EventHistoryItem(BaseModel):
    id: UUID
    title: str
    status: str
    category: str  # UPCOMING | ACTIVE | COMPLETED | CANCELLED
    relationship: str  # OWNER | EVENT_ADMIN | ORGANIZER_STAFF
    visibility: str
    starts_at: datetime
    ends_at: datetime
    timezone: str
    venue_name: str | None
    city: str | None
    capacity: int | None
    tickets_sold: int
    is_suspended: bool
    cancelled_at: datetime | None
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime


class EventHistoryPage(BaseModel):
    items: list[EventHistoryItem]
    total: int
    limit: int
    offset: int


class TimelineActor(BaseModel):
    user_id: UUID | None
    type: str
    display_name: str | None


class TimelineEntry(BaseModel):
    id: int
    timestamp: datetime
    action: str
    entity_type: str
    entity_id: UUID | None
    entity_label: str | None = None
    actor: TimelineActor
    description: str


class TimelinePage(BaseModel):
    items: list[TimelineEntry]
    total: int
    limit: int
    offset: int
