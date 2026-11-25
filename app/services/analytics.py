"""Organizer analytics, event history and the event activity timeline (SCRUM-17).

Every figure is aggregated in PostgreSQL from authoritative rows (orders,
order_items, refunds, tickets, checkin_records, inventory_holds,
promo_redemptions, audit_logs) with plain SELECTs. Nothing here takes a row
lock or writes, so analytics never block checkout or ticket issuance (MVCC
readers do not wait for writers). The number of queries is fixed per request
(grouped aggregates, no per-row lookups).

Time basis (see ``DEFINITIONS``):
* sales, discounts, revenue, campaign sales -> ``orders.paid_at`` (payment
  confirmation, or confirmation time of a free registration);
* refunds -> ``refunds.completed_at`` of SUCCEEDED refunds;
* period attendance -> ``checkin_records.created_at`` / ``tickets.checked_in_at``;
* capacity, availability and attendance rate are current snapshots and ignore
  the date range.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Date, and_, case, func, or_, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.errors import ApiError
from app.db import repositories as repo
from app.db.models import (
    AuditLog,
    Campaign,
    CheckinAction,
    CheckinRecord,
    Event,
    EventStaff,
    EventStaffRole,
    EventStatus,
    HoldStatus,
    InventoryHold,
    Order,
    OrderItem,
    OrderStatus,
    Payment,
    PaymentDispute,
    PromoRedemption,
    Refund,
    RefundStatus,
    SupportCase,
    Ticket,
    TicketStatus,
    TicketType,
    User,
    UserRole,
)
from app.schemas.analytics import (
    AnalyticsResponse,
    AnalyticsScope,
    AttendanceMetrics,
    CampaignMetrics,
    CampaignSales,
    CapacitySnapshot,
    CurrencySales,
    EventHistoryItem,
    EventHistoryPage,
    SalesOverTimePoint,
    TicketTypeMetrics,
    TimelineActor,
    TimelineEntry,
    TimelinePage,
)

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
_COMMITTED = (OrderStatus.PAID, OrderStatus.CONFIRMED)
_REVERSED = (OrderStatus.CANCELLED, OrderStatus.REFUNDED)

# Staff permission key granting analytics. Existing staff defaults
# (EVENT_ADMIN: view_event, check_in_tickets) do not include it, so staff get
# analytics only when explicitly granted.
ANALYTICS_PERMISSION = "view_analytics"
# Staff roles that "manage" an event for the event history listing.
HISTORY_STAFF_ROLES = (EventStaffRole.EVENT_ADMIN, EventStaffRole.ORGANIZER_STAFF)

DEFINITIONS: dict[str, str] = {
    "capacity": (
        "Snapshot, ignores the date range. total_capacity is the sum over events of "
        "min(event capacity, sum of ticket type capacities) (ticket type capacity when "
        "filtered). sold_tickets = order items of PAID/CONFIRMED orders; reserved_tickets "
        "= active unexpired holds; remaining = capacity - sold - reserved (never < 0)."
    ),
    "attendance": (
        "checked_in / eligible_tickets / attendance_rate are snapshots of ticket status: "
        "eligible = VALID + CHECKED_IN (cancelled and refunded tickets excluded), a "
        "reversed check-in returns the ticket to VALID, and a repeated scan cannot "
        "check a ticket in twice. period_check_ins / period_reversals count "
        "checkin_records by created_at; period_admitted_tickets counts distinct tickets "
        "currently CHECKED_IN whose checked_in_at is in the range."
    ),
    "sales": (
        "Realized sales by orders.paid_at (payment confirmation; confirmation time for "
        "free registrations). Pending, expired, failed and abandoned checkouts never have "
        "paid_at and are excluded. Grouped per currency; currencies are never combined."
    ),
    "gross_ticket_sales": "Sum of list prices (order_items.unit_price) of sold items.",
    "discounts": (
        "Sum of order_items.discount_amount of sold items. Informational: already "
        "deducted from ticket_revenue/gross_revenue and never subtracted again."
    ),
    "ticket_revenue": "gross_ticket_sales - discounts (sum of order_items.final_price).",
    "fees": (
        "Processing fees charged on those orders (orders.fee_amount). Order level, so "
        "null when a ticket type filter is applied."
    ),
    "gross_revenue": (
        "Amount charged to customers: ticket_revenue + fees (sum of orders.total_amount). "
        "With a ticket type filter: ticket_revenue of that type."
    ),
    "refunds": (
        "SUCCEEDED refunds by refunds.completed_at (full refunds of the amount charged). "
        "With a ticket type filter: final_price of that type's items in refunded orders."
    ),
    "net_revenue": "gross_revenue - refunds for the same period and currency.",
    "tickets_reversed": (
        "Items sold in the period whose order is now CANCELLED or REFUNDED."
    ),
    "campaigns": (
        "Redemption counts are a snapshot of promo_redemptions (RESERVED / REDEEMED / "
        "RELEASED). Campaign sales are orders attributed to the campaign by "
        "orders.campaign_id, by paid_at; revenue = sum of charged amounts (final_price "
        "of the filtered type when a ticket type filter is applied)."
    ),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc_param(value: datetime | None) -> datetime | None:
    """Query parameters without an offset are interpreted as UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def validate_range(
    start: datetime | None, end: datetime | None
) -> tuple[datetime | None, datetime | None]:
    start, end = as_utc_param(start), as_utc_param(end)
    if start is not None and end is not None and end <= start:
        raise ApiError(422, "INVALID_DATE_RANGE", "end must be after start")
    return start, end


def _money(value: Any) -> Decimal:
    if value is None:
        return ZERO
    return Decimal(str(value)).quantize(CENT)


def _in_range(column: Any, start: datetime | None, end: datetime | None) -> list[Any]:
    conditions = []
    if start is not None:
        conditions.append(column >= start)
    if end is not None:
        conditions.append(column < end)
    return conditions


async def _fetch(db: AsyncSession, stmt: Any) -> list[Any]:
    """All rows of a query, typed as ``Any``.

    SQLAlchemy 2.1 types unpacked multi-column result rows as ``object``; the
    row shape of every call is fixed by the SELECT list right next to it.
    """
    return list((await db.execute(stmt)).all())


# ---------- Authorization scope ----------

def has_analytics_permission(permissions: dict[str, Any] | None) -> bool:
    return bool((permissions or {}).get(ANALYTICS_PERMISSION) is True)


async def analytics_event_filter(db: AsyncSession, user: User) -> Any:
    """Events whose analytics ``user`` may see: owned, or staff with permission.

    Deliberately relationship based: a global role (even PLATFORM_ADMIN) does
    not widen the organizer's aggregated scope.
    """
    staff_rows = await _fetch(db, 
            select(EventStaff.event_id, EventStaff.permissions).where(
                EventStaff.user_id == user.id
            )
        )
    staff_ids = [event_id for event_id, perms in staff_rows if has_analytics_permission(perms)]
    clauses = [Event.owner_user_id == user.id]
    if staff_ids:
        clauses.append(Event.id.in_(staff_ids))
    return or_(*clauses)


async def resolve_scope(
    db: AsyncSession,
    *,
    event_filter: Any,
    event_id: UUID | None,
    ticket_type_id: UUID | None,
) -> tuple[list[UUID], TicketType | None]:
    """Authorize every reference before any aggregate is computed."""
    if event_id is not None:
        allowed = await db.scalar(
            select(Event.id).where(Event.id == event_id, event_filter)
        )
        if allowed is None:
            if await repo.get_event_by_id(db, event_id) is None:
                raise ApiError(404, "EVENT_NOT_FOUND", "Event not found")
            raise ApiError(
                403, "INSUFFICIENT_EVENT_PERMISSIONS", "Insufficient permissions for this event"
            )
        event_ids = [event_id]
    else:
        event_ids = list(
            (await db.execute(select(Event.id).where(event_filter))).scalars().all()
        )

    ticket_type: TicketType | None = None
    if ticket_type_id is not None:
        ticket_type = await repo.get_ticket_type_by_id(db, ticket_type_id)
        # Unknown and out-of-scope ticket types are indistinguishable.
        if ticket_type is None or ticket_type.event_id not in set(event_ids):
            raise ApiError(404, "TICKET_TYPE_NOT_FOUND", "Ticket type not found")
        event_ids = [ticket_type.event_id]
    return event_ids, ticket_type


# ---------- Analytics ----------

@dataclass
class _CurrencyAcc:
    orders: int = 0
    tickets_sold: int = 0
    tickets_reversed: int = 0
    gross_ticket_sales: Decimal = ZERO
    discounts: Decimal = ZERO
    ticket_revenue: Decimal = ZERO
    fees: Decimal = ZERO
    order_total: Decimal = ZERO
    refund_count: int = 0
    refunds: Decimal = ZERO


@dataclass
class _TypeAcc:
    sold: int = 0
    reserved: int = 0
    statuses: dict[TicketStatus, int] = field(default_factory=dict)
    period_sold: int = 0
    period_reversed: int = 0
    period_gross: Decimal = ZERO
    period_discounts: Decimal = ZERO
    period_revenue: Decimal = ZERO


def resolve_dates(
    date_from: date | None, date_to: date | None, tz_name: str
) -> tuple[datetime | None, datetime | None]:
    """Inclusive calendar dates in ``tz_name`` -> UTC [start, end) bounds."""
    if date_from is not None and date_to is not None and date_to < date_from:
        raise ApiError(422, "INVALID_DATE_RANGE", "to must not be before from")
    tz = zone(tz_name)
    start = (
        datetime.combine(date_from, time.min, tzinfo=tz).astimezone(timezone.utc)
        if date_from is not None else None
    )
    end = (
        datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=tz).astimezone(timezone.utc)
        if date_to is not None else None
    )
    return start, end


def zone(tz_name: str) -> tzinfo:
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


async def compute_analytics(
    db: AsyncSession,
    *,
    event_ids: list[UUID],
    ticket_type: TicketType | None,
    start: datetime | None,
    end: datetime | None,
    tz_name: str = "UTC",
) -> AnalyticsResponse:
    now = _now()
    tt_id = ticket_type.id if ticket_type is not None else None
    scope = AnalyticsScope(
        event_ids=event_ids,
        event_count=len(event_ids),
        ticket_type_id=tt_id,
        start=start,
        end=end,
        timezone=tz_name,
        generated_at=now,
    )
    if not event_ids:
        return AnalyticsResponse(
            scope=scope,
            definitions=DEFINITIONS,
            capacity=CapacitySnapshot(
                total_capacity=0, sold_tickets=0, reserved_tickets=0, remaining_tickets=0
            ),
            attendance=AttendanceMetrics(
                checked_in=0, eligible_tickets=0, attendance_rate=None, cancelled_tickets=0,
                refunded_tickets=0, period_check_ins=0, period_reversals=0,
                period_admitted_tickets=0,
            ),
            sales=[],
            ticket_types=[],
            campaigns=[],
        )

    def item_filter() -> list[Any]:
        conditions: list[Any] = [Order.event_id.in_(event_ids)]
        if tt_id is not None:
            conditions.append(OrderItem.ticket_type_id == tt_id)
        return conditions

    # --- ticket types in scope
    tt_stmt = select(TicketType).where(TicketType.event_id.in_(event_ids))
    if tt_id is not None:
        tt_stmt = tt_stmt.where(TicketType.id == tt_id)
    ticket_types = list(
        (await db.execute(tt_stmt.order_by(TicketType.created_at, TicketType.id))).scalars().all()
    )
    per_type: dict[UUID, _TypeAcc] = {t.id: _TypeAcc() for t in ticket_types}

    # --- snapshot: sold (committed) per ticket type
    for type_id, count in await _fetch(db, 
            select(OrderItem.ticket_type_id, func.count(OrderItem.id))
            .join(Order, Order.id == OrderItem.order_id)
            .where(*item_filter(), Order.status.in_(_COMMITTED))
            .group_by(OrderItem.ticket_type_id)
        ):
        if type_id in per_type:
            per_type[type_id].sold = int(count)

    # --- snapshot: active holds per ticket type
    hold_stmt = (
        select(InventoryHold.ticket_type_id, func.coalesce(func.sum(InventoryHold.quantity), 0))
        .where(
            InventoryHold.event_id.in_(event_ids),
            InventoryHold.status == HoldStatus.ACTIVE,
            InventoryHold.expires_at > now,
        )
        .group_by(InventoryHold.ticket_type_id)
    )
    if tt_id is not None:
        hold_stmt = hold_stmt.where(InventoryHold.ticket_type_id == tt_id)
    for type_id, quantity in await _fetch(db, hold_stmt):
        if type_id in per_type:
            per_type[type_id].reserved = int(quantity)

    # --- snapshot: ticket statuses per ticket type
    for type_id, status, count in await _fetch(db, 
            select(OrderItem.ticket_type_id, Ticket.status, func.count(Ticket.id))
            .join(OrderItem, OrderItem.id == Ticket.order_item_id)
            .join(Order, Order.id == OrderItem.order_id)
            .where(*item_filter())
            .group_by(OrderItem.ticket_type_id, Ticket.status)
        ):
        if type_id in per_type:
            per_type[type_id].statuses[status] = int(count)

    # --- period: realized sales per (currency, ticket type)
    reversed_flag = case((Order.status.in_(_REVERSED), 1), else_=0)
    currencies: dict[str, _CurrencyAcc] = defaultdict(_CurrencyAcc)
    for currency, type_id, count, gross, discount, final, reversed_count in await _fetch(db, 
            select(
                Order.currency,
                OrderItem.ticket_type_id,
                func.count(OrderItem.id),
                func.coalesce(func.sum(OrderItem.unit_price), 0),
                func.coalesce(func.sum(OrderItem.discount_amount), 0),
                func.coalesce(func.sum(OrderItem.final_price), 0),
                func.coalesce(func.sum(reversed_flag), 0),
            )
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                *item_filter(),
                Order.paid_at.is_not(None),
                *_in_range(Order.paid_at, start, end),
            )
            .group_by(Order.currency, OrderItem.ticket_type_id)
        ):
        acc = currencies[currency]
        acc.tickets_sold += int(count)
        acc.tickets_reversed += int(reversed_count)
        acc.gross_ticket_sales += _money(gross)
        acc.discounts += _money(discount)
        acc.ticket_revenue += _money(final)
        if type_id in per_type:
            t = per_type[type_id]
            t.period_sold += int(count)
            t.period_reversed += int(reversed_count)
            t.period_gross += _money(gross)
            t.period_discounts += _money(discount)
            t.period_revenue += _money(final)

    # --- period: orders, fees and charged totals (order level)
    order_stmt = select(
        Order.currency,
        func.count(func.distinct(Order.id)),
    ).where(
        Order.event_id.in_(event_ids),
        Order.paid_at.is_not(None),
        *_in_range(Order.paid_at, start, end),
    )
    if tt_id is not None:
        order_stmt = order_stmt.where(
            Order.id.in_(select(OrderItem.order_id).where(OrderItem.ticket_type_id == tt_id))
        )
        for currency, orders in await _fetch(db, order_stmt.group_by(Order.currency)):
            currencies[currency].orders = int(orders)
    else:
        for currency, orders, fees, total in await _fetch(db, 
                order_stmt.add_columns(
                    func.coalesce(func.sum(Order.fee_amount), 0),
                    func.coalesce(func.sum(Order.total_amount), 0),
                ).group_by(Order.currency)
            ):
            acc = currencies[currency]
            acc.orders = int(orders)
            acc.fees = _money(fees)
            acc.order_total = _money(total)

    # --- period: completed refunds
    refund_conditions = [
        Order.event_id.in_(event_ids),
        Refund.status == RefundStatus.SUCCEEDED,
        Refund.completed_at.is_not(None),
        *_in_range(Refund.completed_at, start, end),
    ]
    if tt_id is None:
        for currency, count, amount in await _fetch(db, 
                select(
                    Refund.currency,
                    func.count(Refund.id),
                    func.coalesce(func.sum(Refund.amount), 0),
                )
                .join(Order, Order.id == Refund.order_id)
                .where(*refund_conditions)
                .group_by(Refund.currency)
            ):
            currencies[currency].refund_count = int(count)
            currencies[currency].refunds = _money(amount)
    else:
        # Refunds are full-order, so every item of the type in a refunded
        # order was refunded at its final price.
        for currency, count, amount in await _fetch(db, 
                select(
                    Order.currency,
                    func.count(func.distinct(Refund.id)),
                    func.coalesce(func.sum(OrderItem.final_price), 0),
                )
                .select_from(Refund)
                .join(Order, Order.id == Refund.order_id)
                .join(OrderItem, OrderItem.order_id == Order.id)
                .where(*refund_conditions, OrderItem.ticket_type_id == tt_id)
                .group_by(Order.currency)
            ):
            currencies[currency].refund_count = int(count)
            currencies[currency].refunds = _money(amount)

    sales = []
    for currency in sorted(currencies):
        acc = currencies[currency]
        gross_revenue = acc.ticket_revenue if tt_id is not None else acc.order_total
        sales.append(
            CurrencySales(
                currency=currency,
                orders=acc.orders,
                tickets_sold=acc.tickets_sold,
                tickets_reversed=acc.tickets_reversed,
                gross_ticket_sales=acc.gross_ticket_sales,
                discounts=acc.discounts,
                ticket_revenue=acc.ticket_revenue,
                fees=None if tt_id is not None else acc.fees,
                gross_revenue=gross_revenue,
                refund_count=acc.refund_count,
                refunds=acc.refunds,
                net_revenue=gross_revenue - acc.refunds,
            )
        )

    # --- period attendance
    checkin_stmt = (
        select(CheckinRecord.action, func.count(CheckinRecord.id))
        .where(
            CheckinRecord.event_id.in_(event_ids),
            *_in_range(CheckinRecord.created_at, start, end),
        )
        .group_by(CheckinRecord.action)
    )
    if tt_id is not None:
        checkin_stmt = checkin_stmt.where(
            CheckinRecord.ticket_id.in_(
                select(Ticket.id)
                .join(OrderItem, OrderItem.id == Ticket.order_item_id)
                .where(OrderItem.ticket_type_id == tt_id)
            )
        )
    scans = {action: int(count) for action, count in await _fetch(db, checkin_stmt)}
    admitted = int(
        await db.scalar(
            select(func.count(func.distinct(Ticket.id)))
            .join(OrderItem, OrderItem.id == Ticket.order_item_id)
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                *item_filter(),
                Ticket.status == TicketStatus.CHECKED_IN,
                *_in_range(Ticket.checked_in_at, start, end),
            )
        )
        or 0
    )

    # --- capacity snapshot (events)
    events = list(
        (await db.execute(select(Event).where(Event.id.in_(event_ids)))).scalars().all()
    )
    types_by_event: dict[UUID, list[TicketType]] = defaultdict(list)
    for ticket_type_row in ticket_types:
        types_by_event[ticket_type_row.event_id].append(ticket_type_row)
    total_capacity = sold_total = reserved_total = remaining_total = 0
    for event in events:
        types = types_by_event.get(event.id, [])
        type_capacity = sum(t.capacity for t in types)
        sold = sum(per_type[t.id].sold for t in types)
        reserved = sum(per_type[t.id].reserved for t in types)
        type_remaining = sum(
            max(t.capacity - per_type[t.id].sold - per_type[t.id].reserved, 0) for t in types
        )
        if tt_id is None and event.capacity is not None:
            capacity = min(event.capacity, type_capacity)
            remaining = min(type_remaining, max(event.capacity - sold - reserved, 0))
        else:
            capacity, remaining = type_capacity, type_remaining
        total_capacity += capacity
        sold_total += sold
        reserved_total += reserved
        remaining_total += remaining

    status_totals: dict[TicketStatus, int] = defaultdict(int)
    for type_acc in per_type.values():
        for status, count in type_acc.statuses.items():
            status_totals[status] += count
    checked_in = status_totals[TicketStatus.CHECKED_IN]
    eligible = checked_in + status_totals[TicketStatus.VALID]

    type_metrics = [
        TicketTypeMetrics(
            ticket_type_id=t.id,
            event_id=t.event_id,
            name=t.name,
            currency=t.currency,
            price=_money(t.price),
            is_hidden=t.is_hidden,
            capacity=t.capacity,
            sold_tickets=per_type[t.id].sold,
            reserved_tickets=per_type[t.id].reserved,
            remaining_tickets=max(t.capacity - per_type[t.id].sold - per_type[t.id].reserved, 0),
            checked_in=per_type[t.id].statuses.get(TicketStatus.CHECKED_IN, 0),
            period_tickets_sold=per_type[t.id].period_sold,
            period_tickets_reversed=per_type[t.id].period_reversed,
            period_gross_sales=per_type[t.id].period_gross,
            period_discounts=per_type[t.id].period_discounts,
            period_revenue=per_type[t.id].period_revenue,
        )
        for t in ticket_types
    ]

    return AnalyticsResponse(
        scope=scope,
        definitions=DEFINITIONS,
        capacity=CapacitySnapshot(
            total_capacity=total_capacity,
            sold_tickets=sold_total,
            reserved_tickets=reserved_total,
            remaining_tickets=remaining_total,
        ),
        attendance=AttendanceMetrics(
            checked_in=checked_in,
            eligible_tickets=eligible,
            attendance_rate=round(checked_in / eligible, 4) if eligible else None,
            cancelled_tickets=status_totals[TicketStatus.CANCELLED],
            refunded_tickets=status_totals[TicketStatus.REFUNDED],
            period_check_ins=scans.get(CheckinAction.CHECK_IN, 0),
            period_reversals=scans.get(CheckinAction.REVERSE, 0),
            period_admitted_tickets=admitted,
        ),
        sales=sales,
        sales_over_time=[
            point for _currency, point in await _sales_over_time(
                db, event_ids=event_ids, tt_id=tt_id, start=start, end=end, tz_name=tz_name
            )
        ],
        ticket_types=type_metrics,
        campaigns=await _campaign_metrics(
            db, event_ids=event_ids, tt_id=tt_id, start=start, end=end
        ),
    )


async def _sales_over_time(
    db: AsyncSession,
    *,
    event_ids: list[UUID],
    tt_id: UUID | None,
    start: datetime | None,
    end: datetime | None,
    tz_name: str,
) -> list[tuple[str, SalesOverTimePoint]]:
    """Daily (currency, point) rows by orders.paid_at in ``tz_name``.

    PostgreSQL buckets in SQL with ``timezone()``; the SQLite test fallback
    has no time zone support, so per-order rows are bucketed in Python.
    """
    conditions: list[Any] = [
        Order.event_id.in_(event_ids),
        Order.paid_at.is_not(None),
        *_in_range(Order.paid_at, start, end),
    ]
    if tt_id is not None:
        conditions.append(OrderItem.ticket_type_id == tt_id)
    buckets: dict[tuple[str, date], list[Any]] = {}
    if db.get_bind().dialect.name == "postgresql":
        day = sql_cast(func.timezone(tz_name, Order.paid_at), Date)
        rows = await _fetch(db, 
                select(
                    Order.currency, day, func.count(func.distinct(Order.id)),
                    func.count(OrderItem.id), func.coalesce(func.sum(OrderItem.unit_price), 0),
                )
                .join(Order, Order.id == OrderItem.order_id)
                .where(*conditions)
                .group_by(Order.currency, day)
            )
        for currency, bucket, orders, tickets, gross in rows:
            buckets[(currency, bucket)] = [int(orders), int(tickets), _money(gross)]
    else:
        tz = zone(tz_name)
        order_rows = await _fetch(db, 
                select(
                    Order.id, Order.currency, Order.paid_at,
                    func.count(OrderItem.id), func.coalesce(func.sum(OrderItem.unit_price), 0),
                )
                .join(Order, Order.id == OrderItem.order_id)
                .where(*conditions)
                .group_by(Order.id, Order.currency, Order.paid_at)
            )
        for _order_id, currency, paid_at, tickets, gross in order_rows:
            assert paid_at is not None
            aware = paid_at if paid_at.tzinfo else paid_at.replace(tzinfo=timezone.utc)
            key = (currency, aware.astimezone(tz).date())
            acc = buckets.setdefault(key, [0, 0, ZERO])
            acc[0] += 1
            acc[1] += int(tickets)
            acc[2] += _money(gross)
    return [
        (currency, SalesOverTimePoint(
            date=day_, currency=currency, orders=v[0], tickets=v[1], gross=v[2]
        ))
        for (currency, day_), v in sorted(buckets.items(), key=lambda kv: (kv[0][1], kv[0][0]))
    ]


async def _campaign_metrics(
    db: AsyncSession,
    *,
    event_ids: list[UUID],
    tt_id: UUID | None,
    start: datetime | None,
    end: datetime | None,
) -> list[CampaignMetrics]:
    campaigns = list(
        (
            await db.execute(
                select(Campaign)
                .where(Campaign.event_id.in_(event_ids))
                .order_by(Campaign.created_at, Campaign.id)
            )
        ).scalars().all()
    )
    if not campaigns:
        return []
    campaign_ids = [c.id for c in campaigns]

    redemptions: dict[UUID, dict[str, int]] = defaultdict(dict)
    for campaign_id, status, count in await _fetch(db, 
            select(
                PromoRedemption.campaign_id,
                PromoRedemption.status,
                func.count(PromoRedemption.id),
            )
            .where(PromoRedemption.campaign_id.in_(campaign_ids))
            .group_by(PromoRedemption.campaign_id, PromoRedemption.status)
        ):
        redemptions[campaign_id][status] = int(count)

    paid_in_range = [
        Order.campaign_id.in_(campaign_ids),
        Order.paid_at.is_not(None),
        *_in_range(Order.paid_at, start, end),
    ]
    item_rows: dict[tuple[UUID, str], tuple[int, Decimal, Decimal]] = {}
    item_stmt = (
        select(
            Order.campaign_id,
            Order.currency,
            func.count(OrderItem.id),
            func.coalesce(func.sum(OrderItem.discount_amount), 0),
            func.coalesce(func.sum(OrderItem.final_price), 0),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .where(*paid_in_range)
        .group_by(Order.campaign_id, Order.currency)
    )
    if tt_id is not None:
        item_stmt = item_stmt.where(OrderItem.ticket_type_id == tt_id)
    for campaign_id, currency, count, discount, final in await _fetch(db, item_stmt):
        item_rows[(campaign_id, currency)] = (int(count), _money(discount), _money(final))

    order_stmt = select(
        Order.campaign_id,
        Order.currency,
        func.count(Order.id),
        func.coalesce(func.sum(case((Order.status.in_(_REVERSED), 1), else_=0)), 0),
        func.coalesce(func.sum(Order.total_amount), 0),
    ).where(*paid_in_range)
    if tt_id is not None:
        order_stmt = order_stmt.where(
            Order.id.in_(select(OrderItem.order_id).where(OrderItem.ticket_type_id == tt_id))
        )
    grouped = await _fetch(db, order_stmt.group_by(Order.campaign_id, Order.currency))
    order_rows = {
        (campaign_id, currency): (int(orders), int(reversed_count), _money(total))
        for campaign_id, currency, orders, reversed_count, total in grouped
    }

    result = []
    for campaign in campaigns:
        keys = sorted(
            {key for key in [*item_rows, *order_rows] if key[0] == campaign.id},
            key=lambda key: key[1],
        )
        sales = []
        for key in keys:
            tickets, discount, final = item_rows.get(key, (0, ZERO, ZERO))
            orders, reversed_count, total = order_rows.get(key, (0, 0, ZERO))
            sales.append(
                CampaignSales(
                    currency=key[1],
                    orders=orders,
                    tickets_sold=tickets,
                    reversed_orders=reversed_count,
                    discounts=discount,
                    revenue=final if tt_id is not None else total,
                )
            )
        counts = redemptions.get(campaign.id, {})
        result.append(
            CampaignMetrics(
                campaign_id=campaign.id,
                event_id=campaign.event_id,
                name=campaign.name,
                promo_code=campaign.promo_code,
                status=campaign.status.value,
                reserved_redemptions=counts.get("RESERVED", 0),
                successful_redemptions=counts.get("REDEEMED", 0),
                released_redemptions=counts.get("RELEASED", 0),
                sales=sales,
            )
        )
    return result


# ---------- Event history ----------

CATEGORIES = ("UPCOMING", "ACTIVE", "COMPLETED", "CANCELLED")


def category_expression(now: datetime) -> Any:
    """Lifecycle category from status + schedule. Every event gets exactly one.

    CANCELLED  status CANCELLED
    COMPLETED  status COMPLETED, or the event has ended
    ACTIVE     started and not yet ended
    UPCOMING   not started yet (includes drafts and unpublished events)

    Suspension is orthogonal (``is_suspended``) and never changes the category.
    """
    return case(
        (Event.status == EventStatus.CANCELLED, "CANCELLED"),
        (or_(Event.status == EventStatus.COMPLETED, Event.ends_at <= now), "COMPLETED"),
        (Event.starts_at <= now, "ACTIVE"),
        else_="UPCOMING",
    )


def categorize(event: Event, now: datetime) -> str:
    if event.status == EventStatus.CANCELLED:
        return "CANCELLED"
    if event.status == EventStatus.COMPLETED or event.ends_at <= now:
        return "COMPLETED"
    if event.starts_at <= now:
        return "ACTIVE"
    return "UPCOMING"


async def event_history(
    db: AsyncSession,
    user: User,
    *,
    categories: list[str] | None,
    statuses: list[EventStatus] | None,
    q: str | None,
    suspended: bool | None,
    starts_from: datetime | None,
    starts_to: datetime | None,
    sort: str,
    limit: int,
    offset: int,
) -> EventHistoryPage:
    now = _now()
    starts_from, starts_to = validate_range(starts_from, starts_to)
    staff = aliased(EventStaff)
    category = category_expression(now)
    conditions: list[Any] = [
        or_(Event.owner_user_id == user.id, staff.user_id.is_not(None)),
    ]
    if categories:
        conditions.append(category.in_(categories))
    if statuses:
        conditions.append(Event.status.in_(statuses))
    if q:
        pattern = f"%{repo._escape_like(q.strip())}%"
        conditions.append(
            or_(
                Event.title.ilike(pattern, escape="\\"),
                Event.venue_name.ilike(pattern, escape="\\"),
                Event.city.ilike(pattern, escape="\\"),
            )
        )
    if suspended is True:
        conditions.append(Event.suspended_at.is_not(None))
    elif suspended is False:
        conditions.append(Event.suspended_at.is_(None))
    if starts_from is not None:
        conditions.append(Event.starts_at >= starts_from)
    if starts_to is not None:
        conditions.append(Event.starts_at < starts_to)

    staff_join = and_(
        staff.event_id == Event.id,
        staff.user_id == user.id,
        staff.role.in_(HISTORY_STAFF_ROLES),
    )
    total = int(
        await db.scalar(
            select(func.count(Event.id)).outerjoin(staff, staff_join).where(*conditions)
        )
        or 0
    )
    sold = (
        select(func.count(OrderItem.id))
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.event_id == Event.id, Order.status.in_(_COMMITTED))
        .correlate(Event)
        .scalar_subquery()
    )
    columns = {
        "starts_at": Event.starts_at,
        "created_at": Event.created_at,
        "title": Event.title,
    }
    column = columns[sort.lstrip("-")]
    descending = sort.startswith("-")
    rows = await _fetch(db, 
            select(Event, staff.role, sold)
            .outerjoin(staff, staff_join)
            .where(*conditions)
            .order_by(
                column.desc() if descending else column.asc(),
                Event.id.desc() if descending else Event.id.asc(),
            )
            .limit(limit)
            .offset(offset)
        )
    items = [
        EventHistoryItem(
            id=event.id,
            title=event.title,
            status=event.status.value,
            category=categorize(event, now),
            relationship="OWNER" if event.owner_user_id == user.id else staff_role.value,
            visibility=event.visibility.value,
            starts_at=event.starts_at,
            ends_at=event.ends_at,
            timezone=event.timezone,
            venue_name=event.venue_name,
            city=event.city,
            capacity=event.capacity,
            tickets_sold=int(sold_count or 0),
            is_suspended=event.suspended_at is not None,
            cancelled_at=event.cancelled_at,
            published_at=event.published_at,
            created_at=event.created_at,
            updated_at=event.updated_at,
        )
        for event, staff_role, sold_count in rows
    ]
    return EventHistoryPage(items=items, total=total, limit=limit, offset=offset)


# ---------- Activity timeline ----------

# Security probes are not event activity and are kept off the timeline.
_TIMELINE_EXCLUDED = ("event.staff_access_denied", "support_case.access_denied")

# Reports are confidential: the organizer must never learn who reported their
# event (nor see the admin's handling of it). These rows stay admin-only.
_OWNER_HIDDEN_ENTITY_TYPES = ("event_report",)


def _owner_visible() -> Any:
    return AuditLog.entity_type.not_in(_OWNER_HIDDEN_ENTITY_TYPES)

_DESCRIPTIONS = {
    "event.created": "Event created",
    "event.updated": "Event details updated",
    "event.published": "Event published",
    "event.unpublished": "Event unpublished",
    "event.cancelled": "Event cancelled",
    "event.completed": "Event completed",
    "event.duplicated": "Event created as a copy of another event",
    "event.deleted": "Draft event deleted",
    "event.refund_policy_updated": "Refund policy updated",
    "event.admin_assigned": "Event admin assigned",
    "event.admin_removed": "Event admin removed",
    "ticket_type.created": "Ticket type created",
    "ticket_type.updated": "Ticket type updated",
    "ticket_type.hidden": "Ticket type hidden",
    "order.created": "Order placed",
    "order.paid": "Order paid",
    "order.cancelled": "Order cancelled",
    "refund.requested": "Refund requested",
    "refund.completed": "Refund completed",
    "refund.denied": "Refund request denied",
    "campaign.created": "Campaign created",
    "campaign.updated": "Campaign updated",
    "ticket.checked_in": "Ticket checked in",
    "ticket.check_in_reversed": "Ticket check-in reversed",
    "support_case.created": "Support case opened",
    "support_case.assigned": "Support case assigned",
    "support_case.unassigned": "Support case unassigned",
    "support_case.escalated": "Support case escalated to Platform Admins",
    "admin.event_suspended": "Event suspended by a Platform Admin",
    "admin.event_unsuspended": "Event suspension lifted by a Platform Admin",
    "admin.paid_sales_stopped": "Paid ticket sales stopped by a Platform Admin",
    "admin.paid_sales_resumed": "Paid ticket sales resumed by a Platform Admin",
    "admin.dispute_opened": "Payment dispute opened",
    "admin.dispute_updated": "Payment dispute updated",
}


def describe_entry(entry: AuditLog) -> str:
    """Human-readable, redacted description (no raw before/after payloads)."""
    before = entry.before_data or {}
    after = entry.after_data or {}
    if entry.action == "event.updated" and before:
        fields = ", ".join(sorted(key.replace("_", " ") for key in before))
        return f"Event details updated ({fields})"
    if entry.action in ("payment.state_changed", "support_case.status_changed"):
        label = "Payment" if entry.action.startswith("payment") else "Support case status"
        return f"{label} changed from {before.get('status')} to {after.get('status')}"
    if entry.action in _DESCRIPTIONS:
        return _DESCRIPTIONS[entry.action]
    return entry.action.replace(".", ": ").replace("_", " ").capitalize()


async def event_timeline(
    db: AsyncSession,
    event: Event,
    *,
    actions: list[str] | None,
    entity_types: list[str] | None,
    start: datetime | None,
    end: datetime | None,
    order: str,
    limit: int,
    offset: int,
) -> TimelinePage:
    """Event activity from the existing append-only audit log only.

    Nothing is synthesized: actions that were never recorded do not appear.
    ``actions`` entries ending in ``.`` or ``.*`` match a prefix
    (e.g. ``refund.``), anything else matches exactly.
    """
    start, end = validate_range(start, end)
    conditions: list[Any] = [
        AuditLog.event_id == event.id,
        AuditLog.action.not_in(_TIMELINE_EXCLUDED),
        _owner_visible(),
        *_in_range(AuditLog.created_at, start, end),
    ]
    if actions:
        clauses = []
        for action in actions:
            if action.endswith(".*") or action.endswith("."):
                prefix = action.rstrip("*")
                clauses.append(AuditLog.action.startswith(prefix, autoescape=True))
            else:
                clauses.append(AuditLog.action == action)
        conditions.append(or_(*clauses))
    if entity_types:
        conditions.append(AuditLog.entity_type.in_(entity_types))

    total = int(
        await db.scalar(select(func.count()).select_from(AuditLog).where(*conditions)) or 0
    )
    actor = aliased(User)
    ascending = order == "asc"
    rows = await _fetch(db, 
            select(AuditLog, actor)
            .outerjoin(actor, actor.id == AuditLog.actor_user_id)
            .where(*conditions)
            .order_by(
                AuditLog.created_at.asc() if ascending else AuditLog.created_at.desc(),
                AuditLog.id.asc() if ascending else AuditLog.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
    labels = await entity_labels(db, [row[0] for row in rows])
    items = [
        TimelineEntry(
            id=entry.id,
            timestamp=entry.created_at,
            action=entry.action,
            entity_type=entry.entity_type,
            entity_id=entry.entity_id,
            entity_label=(
                labels.get((entry.entity_type, entry.entity_id)) if entry.entity_id else None
            ),
            actor=TimelineActor(
                user_id=entry.actor_user_id,
                type=entry.actor_type,
                display_name=(
                    f"{author.first_name} {author.last_name}".strip() if author else None
                ),
            ),
            description=describe_entry(entry),
        )
        for entry, author in rows
    ]
    return TimelinePage(items=items, total=total, limit=limit, offset=offset)


def is_platform_admin(user: User) -> bool:
    return user.global_role == UserRole.PLATFORM_ADMIN


# ---------- Entity labels (audit log / timeline) ----------

async def entity_labels(db: AsyncSession, entries: list[AuditLog]) -> dict[tuple[str, UUID], str]:
    """Human-readable labels for the entities on one page of audit entries.

    One query per entity type present (bounded by the page), no per-row loads.
    """
    wanted: dict[str, set[UUID]] = defaultdict(set)
    for entry in entries:
        if entry.entity_id is not None:
            wanted[entry.entity_type].add(entry.entity_id)
    labels: dict[tuple[str, UUID], str] = {}

    async def collect(kind: str, stmt: Any, fmt: Any) -> None:
        if not wanted.get(kind):
            return
        for row in await _fetch(db, stmt):
            labels[(kind, row[0])] = fmt(*row[1:])

    def order_label(prefix: str) -> Any:
        return lambda number: f"{prefix}order #{number}" if prefix else f"Order #{number}"

    await collect("event", select(Event.id, Event.title).where(
        Event.id.in_(wanted["event"])), lambda title: title)
    await collect("ticket_type", select(TicketType.id, TicketType.name).where(
        TicketType.id.in_(wanted["ticket_type"])), lambda name: name)
    await collect("campaign", select(Campaign.id, Campaign.name).where(
        Campaign.id.in_(wanted["campaign"])), lambda name: name)
    await collect("support_case", select(SupportCase.id, SupportCase.subject).where(
        SupportCase.id.in_(wanted["support_case"])), lambda subject: subject)
    await collect("order", select(Order.id, Order.order_number).where(
        Order.id.in_(wanted["order"])), order_label(""))
    await collect("payment", select(Payment.id, Order.order_number).join(
        Order, Order.id == Payment.order_id).where(Payment.id.in_(wanted["payment"])),
        order_label("Payment for "))
    await collect("refund", select(Refund.id, Order.order_number).join(
        Order, Order.id == Refund.order_id).where(Refund.id.in_(wanted["refund"])),
        order_label("Refund for "))
    await collect("payment_dispute", select(PaymentDispute.id, Order.order_number).join(
        Order, Order.id == PaymentDispute.order_id).where(
        PaymentDispute.id.in_(wanted["payment_dispute"])), order_label("Dispute on "))
    await collect("ticket", select(Ticket.id, OrderItem.ticket_type_name).join(
        OrderItem, OrderItem.id == Ticket.order_item_id).where(Ticket.id.in_(wanted["ticket"])),
        lambda name: f"{name} ticket")
    for kind in ("user", "event_staff", "organizer_profile"):
        await collect(kind, select(User.id, User.first_name, User.last_name).where(
            User.id.in_(wanted[kind])), lambda first, last: f"{first} {last}".strip())
    for event_id in wanted.get("event_sales_activation", set()):
        labels[("event_sales_activation", event_id)] = "Paid-sales activation"
    return labels


async def event_audit_entries(
    db: AsyncSession,
    event: Event,
    *,
    action: str | None,
    action_prefix: str | None,
    date_from: date | None,
    date_to: date | None,
    limit: int,
    offset: int,
) -> list[tuple[AuditLog, User | None, str | None]]:
    """Owner-facing audit log page (newest first) with actor and entity labels."""
    start, end = resolve_dates(date_from, date_to, event.timezone)
    actor = aliased(User)
    stmt = (
        select(AuditLog, actor)
        .outerjoin(actor, actor.id == AuditLog.actor_user_id)
        .where(
            AuditLog.event_id == event.id,
            _owner_visible(),
            *_in_range(AuditLog.created_at, start, end),
        )
    )
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if action_prefix:
        stmt = stmt.where(AuditLog.action.startswith(action_prefix, autoescape=True))
    rows = await _fetch(db, 
            stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(limit).offset(offset)
        )
    labels = await entity_labels(db, [row[0] for row in rows])
    return [
        (
            entry,
            author,
            labels.get((entry.entity_type, entry.entity_id)) if entry.entity_id else None,
        )
        for entry, author in rows
    ]
