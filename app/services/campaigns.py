"""Promo-code campaigns (SCRUM-15).

Pricing rules (authoritative, server-side only):

* A campaign applies to the ticket types linked in ``campaign_ticket_types``;
  with no links it applies to every ticket type of its event.
* Discounts touch eligible units only. Ineligible units in the same order are
  charged full price (mixed orders are allowed).
* PERCENT: each eligible unit is discounted by ``unit_price * pct / 100``
  rounded half-up to 0.01 KZT. The order discount is the sum of unit discounts,
  so item rows and order totals always agree.
* FIXED: one KZT amount per order, capped at the eligible subtotal (never
  negative). It is spread over eligible units proportionally to their price
  in whole tiyn; leftover tiyn go to the first units in a stable order
  (ticket type id, then unit index), so the split is deterministic.
* One campaign per order. There are no other discount sources, so stacking is
  impossible by construction.

Redemption accounting: ``campaigns.redemption_count`` counts RESERVED and
REDEEMED rows. It is only changed with conditional UPDATEs (``count < limit``
for reserve; status-guarded transitions for release) and a CHECK constraint
bounds it by ``max_redemptions``, so PostgreSQL enforces the limit even when
checkouts race. Previews and QR resolution are read-only and never consume.
"""

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, and_, case, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db import repositories as repo
from app.db.models import (
    Campaign,
    CampaignStatus,
    CampaignTicketType,
    DiscountType,
    Event,
    EventStatus,
    Order,
    OrderStatus,
    PromoRedemption,
    PromoRedemptionStatus,
    TicketType,
    User,
)
from app.schemas.campaigns import (
    CampaignAnalyticsResponse,
    CampaignCreate,
    CampaignResponse,
    CampaignUpdate,
)

logger = logging.getLogger("app.campaigns")

CENT = Decimal("0.01")
_HOLDING_STATUSES = (
    PromoRedemptionStatus.RESERVED.value,
    PromoRedemptionStatus.REDEEMED.value,
)
_COMMITTED_ORDER_STATUSES = (OrderStatus.PAID, OrderStatus.CONFIRMED)
_REVERSED_ORDER_STATUSES = (
    OrderStatus.CANCELLED,
    OrderStatus.REFUNDED,
    OrderStatus.PARTIALLY_REFUNDED,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_code(code: str) -> str:
    return code.strip().upper()


def qr_path(campaign: Campaign) -> str:
    return f"/campaigns/{campaign.campaign_token}"


def qr_url(campaign: Campaign, link_base: str | None) -> str | None:
    return f"{link_base}{qr_path(campaign)}" if link_base else None


# ---------- State ----------

def effective_status(campaign: Campaign, now: datetime | None = None) -> str:
    now = now or _now()
    if campaign.status == CampaignStatus.EXPIRED:
        return "EXPIRED"
    if campaign.status != CampaignStatus.ACTIVE:
        return "DISABLED"
    if campaign.starts_at is not None and now < campaign.starts_at:
        return "SCHEDULED"
    if campaign.ends_at is not None and now >= campaign.ends_at:
        return "EXPIRED"
    if (
        campaign.max_redemptions is not None
        and campaign.redemption_count >= campaign.max_redemptions
    ):
        return "EXHAUSTED"
    return "ACTIVE"


_STATE_ERRORS = {
    "DISABLED": ("PROMO_DISABLED", "Promo code is disabled"),
    "SCHEDULED": ("PROMO_NOT_STARTED", "Promo code is not active yet"),
    "EXPIRED": ("PROMO_EXPIRED", "Promo code has expired"),
    "EXHAUSTED": ("PROMO_LIMIT_REACHED", "Promo code redemption limit has been reached"),
}


def ensure_usable(campaign: Campaign, now: datetime | None = None) -> None:
    state = effective_status(campaign, now)
    if state != "ACTIVE":
        code, message = _STATE_ERRORS[state]
        raise ApiError(409, code, message)


def _not_found() -> ApiError:
    return ApiError(404, "PROMO_NOT_FOUND", "Promo code not found")


def _not_applicable(message: str = "Promo code does not apply to this order") -> ApiError:
    return ApiError(409, "PROMO_NOT_APPLICABLE", message)


# ---------- Lookup ----------

async def applicable_ticket_type_ids(db: AsyncSession, campaign_id: UUID) -> list[UUID]:
    rows = await db.execute(
        select(CampaignTicketType.ticket_type_id)
        .where(CampaignTicketType.campaign_id == campaign_id)
        .order_by(CampaignTicketType.ticket_type_id)
    )
    return list(rows.scalars().all())


async def find_for_checkout(
    db: AsyncSession,
    *,
    event_id: UUID,
    code: str | None = None,
    campaign_token: UUID | None = None,
) -> Campaign:
    """Resolve and validate the campaign named by a code or a QR token."""
    if code is not None:
        campaign = await db.scalar(
            select(Campaign).where(
                Campaign.event_id == event_id,
                Campaign.promo_code == normalize_code(code),
            )
        )
        if campaign is None:
            raise _not_found()
    elif campaign_token is not None:
        campaign = await repo.get_campaign_by_token(db, campaign_token)
        if campaign is None:
            raise _not_found()
        if campaign.event_id != event_id:
            raise _not_applicable("Promotion belongs to a different event")
    else:  # pragma: no cover - callers guarantee one source
        raise _not_found()
    ensure_usable(campaign)
    return campaign


# ---------- Pricing ----------

@dataclass(frozen=True)
class PriceLine:
    ticket_type_id: UUID
    unit_price: Decimal
    quantity: int


@dataclass
class DiscountResult:
    eligible_subtotal: Decimal
    total: Decimal
    # Per-unit discount amounts, aligned with the unit order of each line.
    per_unit: dict[UUID, list[Decimal]] = field(default_factory=dict)
    eligible_ticket_type_ids: set[UUID] = field(default_factory=set)


def calculate_discount(
    *,
    discount_type: DiscountType,
    discount_value: Decimal,
    lines: list[PriceLine],
    applicable: set[UUID] | None,
    currency: str,
) -> DiscountResult:
    ordered = sorted(lines, key=lambda line: str(line.ticket_type_id))
    eligible_ids = {
        line.ticket_type_id
        for line in ordered
        if not applicable or line.ticket_type_id in applicable
    }
    if not eligible_ids:
        raise _not_applicable("Promo code does not apply to the selected ticket types")
    if discount_type == DiscountType.FIXED and currency != "KZT":
        raise _not_applicable("Fixed discounts are defined in KZT")

    units: list[tuple[UUID, Decimal]] = [
        (line.ticket_type_id, line.unit_price)
        for line in ordered
        if line.ticket_type_id in eligible_ids
        for _ in range(line.quantity)
    ]
    eligible_subtotal = sum((price for _id, price in units), Decimal("0.00"))
    if eligible_subtotal <= 0:
        raise _not_applicable("Selected ticket types are already free")

    if discount_type == DiscountType.PERCENT:
        rate = discount_value / Decimal(100)
        unit_discounts = [
            min(price, (price * rate).quantize(CENT, rounding=ROUND_HALF_UP))
            for _id, price in units
        ]
    else:
        target = min(discount_value, eligible_subtotal)
        unit_discounts = [
            (target * price / eligible_subtotal).quantize(CENT, rounding=ROUND_DOWN)
            for _id, price in units
        ]
        # Floor rounding loses < 1 tiyn per unit, so one pass always suffices.
        remainder = int(((target - sum(unit_discounts, Decimal("0"))) / CENT).to_integral())
        for index, (_id, price) in enumerate(units):
            if remainder <= 0:
                break
            if unit_discounts[index] + CENT <= price:
                unit_discounts[index] += CENT
                remainder -= 1

    result = DiscountResult(
        eligible_subtotal=eligible_subtotal,
        total=sum(unit_discounts, Decimal("0.00")),
        eligible_ticket_type_ids=eligible_ids,
    )
    for (ticket_type_id, _price), amount in zip(units, unit_discounts, strict=True):
        result.per_unit.setdefault(ticket_type_id, []).append(amount)
    return result


# ---------- Redemption accounting ----------

async def reserve_capacity(db: AsyncSession, campaign: Campaign) -> None:
    """Atomically take one unit of campaign capacity or fail.

    The predicate is re-evaluated by PostgreSQL after any concurrent writer
    commits (READ COMMITTED row re-check), so at most ``max_redemptions``
    increments can ever succeed. The CHECK constraint is the final backstop.
    """
    now = _now()
    result = await db.execute(
        update(Campaign)
        .where(
            Campaign.id == campaign.id,
            Campaign.status == CampaignStatus.ACTIVE,
            or_(
                Campaign.max_redemptions.is_(None),
                Campaign.redemption_count < Campaign.max_redemptions,
            ),
        )
        .values(redemption_count=Campaign.redemption_count + 1, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if (cast(CursorResult, result).rowcount or 0) != 1:
        raise ApiError(
            409,
            "PROMO_LIMIT_REACHED",
            "Promo code redemption limit has been reached",
            retryable=False,
        )


async def ensure_no_active_reservation(
    db: AsyncSession, *, campaign_id: UUID, user_id: UUID
) -> None:
    existing = await db.scalar(
        select(PromoRedemption.order_id).where(
            PromoRedemption.campaign_id == campaign_id,
            PromoRedemption.user_id == user_id,
            PromoRedemption.status == PromoRedemptionStatus.RESERVED.value,
        )
    )
    if existing is not None:
        raise ApiError(
            409,
            "PROMO_ALREADY_RESERVED",
            "You already have a pending checkout using this promo code",
            details={"order_id": str(existing)},
        )


async def record_redemption(
    db: AsyncSession,
    *,
    campaign_id: UUID,
    order_id: UUID,
    user_id: UUID,
    discount_amount: Decimal,
    completed: bool,
) -> PromoRedemption:
    now = _now()
    redemption = PromoRedemption(
        campaign_id=campaign_id,
        order_id=order_id,
        user_id=user_id,
        discount_amount=discount_amount,
        status=(
            PromoRedemptionStatus.REDEEMED.value
            if completed
            else PromoRedemptionStatus.RESERVED.value
        ),
        redeemed_at=now,
        updated_at=now,
    )
    try:
        async with db.begin_nested():
            db.add(redemption)
            await db.flush()
    except IntegrityError as exc:
        raise ApiError(
            409,
            "PROMO_ALREADY_RESERVED",
            "You already have a pending checkout using this promo code",
        ) from exc
    return redemption


async def complete_redemption(db: AsyncSession, order_id: UUID) -> None:
    """RESERVED -> REDEEMED once payment succeeds. Capacity is unchanged."""
    await db.execute(
        update(PromoRedemption)
        .where(
            PromoRedemption.order_id == order_id,
            PromoRedemption.status == PromoRedemptionStatus.RESERVED.value,
        )
        .values(status=PromoRedemptionStatus.REDEEMED.value, updated_at=_now())
        .execution_options(synchronize_session=False)
    )


async def release_redemptions(db: AsyncSession, order_ids: list[UUID]) -> int:
    """Return capacity for failed/expired/cancelled/refunded orders.

    The status predicate makes each release happen exactly once, even when a
    retry or a concurrent worker processes the same order. Campaign counters
    are decremented in a stable (id) order to keep lock ordering consistent.
    """
    if not order_ids:
        return 0
    now = _now()
    result = await db.execute(
        update(PromoRedemption)
        .where(
            PromoRedemption.order_id.in_(order_ids),
            PromoRedemption.status.in_(_HOLDING_STATUSES),
        )
        .values(
            status=PromoRedemptionStatus.RELEASED.value,
            released_at=now,
            updated_at=now,
        )
        .returning(PromoRedemption.campaign_id)
        .execution_options(synchronize_session=False)
    )
    released = Counter(result.scalars().all())
    for campaign_id in sorted(released, key=str):
        await db.execute(
            update(Campaign)
            .where(Campaign.id == campaign_id)
            .values(
                redemption_count=Campaign.redemption_count - released[campaign_id],
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
    total = sum(released.values())
    if total:
        logger.info("released %s promo redemption(s)", total)
    return total


# ---------- Organizer management ----------

def _snapshot(campaign: Campaign, ticket_type_ids: list[UUID]) -> dict[str, Any]:
    return {
        "name": campaign.name,
        "code": campaign.promo_code,
        "discount_type": campaign.discount_type.value,
        "discount_value": str(campaign.discount_value),
        "starts_at": campaign.starts_at.isoformat() if campaign.starts_at else None,
        "expires_at": campaign.ends_at.isoformat() if campaign.ends_at else None,
        "redemption_limit": campaign.max_redemptions,
        "status": campaign.status.value,
        "ticket_type_ids": [str(value) for value in ticket_type_ids],
    }


async def _validated_ticket_type_ids(
    db: AsyncSession, event: Event, ids: list[UUID]
) -> list[UUID]:
    unique = sorted(set(ids), key=str)
    if not unique:
        return []
    found = set(
        (
            await db.execute(
                select(TicketType.id).where(
                    TicketType.id.in_(unique), TicketType.event_id == event.id
                )
            )
        ).scalars().all()
    )
    missing = [str(value) for value in unique if value not in found]
    if missing:
        raise ApiError(
            400,
            "INVALID_TICKET_TYPE",
            "Campaign ticket types must belong to the campaign event",
            details={"ticket_type_ids": missing},
        )
    return unique


async def _replace_ticket_types(
    db: AsyncSession, campaign_id: UUID, ticket_type_ids: list[UUID]
) -> None:
    await db.execute(
        delete(CampaignTicketType).where(CampaignTicketType.campaign_id == campaign_id)
    )
    db.add_all(
        CampaignTicketType(campaign_id=campaign_id, ticket_type_id=value)
        for value in ticket_type_ids
    )
    await db.flush()


def _ensure_event_manageable(event: Event) -> None:
    if event.status in (EventStatus.CANCELLED, EventStatus.COMPLETED):
        raise ApiError(
            409,
            "EVENT_NOT_EDITABLE",
            f"Campaigns cannot be managed for a {event.status.value.lower()} event",
        )


async def create_campaign(
    db: AsyncSession, actor: User, event: Event, payload: CampaignCreate
) -> Campaign:
    _ensure_event_manageable(event)
    code = normalize_code(payload.code)
    ticket_type_ids = await _validated_ticket_type_ids(db, event, payload.ticket_type_ids)
    duplicate = await db.scalar(
        select(Campaign.id).where(Campaign.event_id == event.id, Campaign.promo_code == code)
    )
    if duplicate is not None:
        raise ApiError(409, "PROMO_CODE_TAKEN", "This promo code already exists for the event")

    now = _now()
    campaign = Campaign(
        event_id=event.id,
        name=payload.name,
        promo_code=code,
        discount_type=DiscountType(payload.discount_type),
        discount_value=payload.discount_value.quantize(CENT),
        starts_at=payload.starts_at,
        ends_at=payload.expires_at,
        max_redemptions=payload.redemption_limit,
        redemption_count=0,
        status=CampaignStatus.ACTIVE if payload.is_active else CampaignStatus.PAUSED,
        created_at=now,
        updated_at=now,
    )
    try:
        async with db.begin_nested():
            db.add(campaign)
            await db.flush()
    except IntegrityError as exc:
        raise ApiError(
            409, "PROMO_CODE_TAKEN", "This promo code already exists for the event"
        ) from exc
    await _replace_ticket_types(db, campaign.id, ticket_type_ids)
    await repo.add_audit_entry(
        db,
        actor_user_id=actor.id,
        event_id=event.id,
        entity_type="campaign",
        entity_id=campaign.id,
        action="campaign.created",
        after_data=_snapshot(campaign, ticket_type_ids),
    )
    logger.info("campaign %s created for event %s", campaign.id, event.id)
    return campaign


async def get_campaign(
    db: AsyncSession, event: Event, campaign_id: UUID, *, for_update: bool = False
) -> Campaign:
    stmt = select(Campaign).where(Campaign.id == campaign_id, Campaign.event_id == event.id)
    if for_update:
        stmt = stmt.with_for_update()
    campaign = await db.scalar(stmt)
    if campaign is None:
        raise ApiError(404, "CAMPAIGN_NOT_FOUND", "Campaign not found")
    return campaign


async def list_campaigns(db: AsyncSession, event: Event) -> list[Campaign]:
    rows = await db.execute(
        select(Campaign)
        .where(Campaign.event_id == event.id)
        .order_by(Campaign.created_at.desc(), Campaign.id)
    )
    return list(rows.scalars().all())


async def update_campaign(
    db: AsyncSession, actor: User, event: Event, campaign_id: UUID, payload: CampaignUpdate
) -> Campaign:
    _ensure_event_manageable(event)
    campaign = await get_campaign(db, event, campaign_id, for_update=True)
    await db.refresh(campaign)
    before_ids = await applicable_ticket_type_ids(db, campaign.id)
    before = _snapshot(campaign, before_ids)
    fields = payload.model_fields_set

    if fields & {"discount_type", "discount_value"}:
        used = await db.scalar(
            select(func.count())
            .select_from(PromoRedemption)
            .where(PromoRedemption.campaign_id == campaign.id)
        )
        if used:
            raise ApiError(
                409,
                "CAMPAIGN_IN_USE",
                "The discount cannot change after the promo code has been used",
            )
        new_type = DiscountType(payload.discount_type or campaign.discount_type.value)
        new_value = (
            payload.discount_value
            if payload.discount_value is not None
            else campaign.discount_value
        )
        if new_type == DiscountType.PERCENT and new_value > 100:
            raise ApiError(400, "INVALID_DISCOUNT", "Percentage discount must be at most 100")
        campaign.discount_type = new_type
        campaign.discount_value = new_value.quantize(CENT)

    if "name" in fields and payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise ApiError(400, "VALIDATION_ERROR", "name must not be blank")
        campaign.name = name
    if "starts_at" in fields:
        campaign.starts_at = payload.starts_at
    if "expires_at" in fields:
        campaign.ends_at = payload.expires_at
    if (
        campaign.starts_at is not None
        and campaign.ends_at is not None
        and campaign.ends_at <= campaign.starts_at
    ):
        raise ApiError(400, "INVALID_CAMPAIGN_WINDOW", "expires_at must be after starts_at")
    if "redemption_limit" in fields:
        if (
            payload.redemption_limit is not None
            and payload.redemption_limit < campaign.redemption_count
        ):
            raise ApiError(
                409,
                "REDEMPTION_LIMIT_BELOW_USAGE",
                "Redemption limit cannot be lower than current usage",
                details={"redemption_count": campaign.redemption_count},
            )
        campaign.max_redemptions = payload.redemption_limit
    if "is_active" in fields and payload.is_active is not None:
        campaign.status = CampaignStatus.ACTIVE if payload.is_active else CampaignStatus.PAUSED

    after_ids = before_ids
    if "ticket_type_ids" in fields and payload.ticket_type_ids is not None:
        after_ids = await _validated_ticket_type_ids(db, event, payload.ticket_type_ids)
        await _replace_ticket_types(db, campaign.id, after_ids)

    campaign.updated_at = _now()
    await db.flush()
    after = _snapshot(campaign, after_ids)
    if after != before:
        await repo.add_audit_entry(
            db,
            actor_user_id=actor.id,
            event_id=event.id,
            entity_type="campaign",
            entity_id=campaign.id,
            action="campaign.updated",
            before_data=before,
            after_data=after,
        )
    return campaign


async def build_response(
    db: AsyncSession, campaign: Campaign, *, link_base: str | None
) -> CampaignResponse:
    ticket_type_ids = await applicable_ticket_type_ids(db, campaign.id)
    remaining = (
        max(0, campaign.max_redemptions - campaign.redemption_count)
        if campaign.max_redemptions is not None
        else None
    )
    return CampaignResponse(
        id=campaign.id,
        event_id=campaign.event_id,
        name=campaign.name,
        code=campaign.promo_code,
        discount_type=campaign.discount_type.value,
        discount_value=campaign.discount_value,
        starts_at=campaign.starts_at,
        expires_at=campaign.ends_at,
        redemption_limit=campaign.max_redemptions,
        redemption_count=campaign.redemption_count,
        remaining_redemptions=remaining,
        ticket_type_ids=ticket_type_ids,
        is_active=campaign.status == CampaignStatus.ACTIVE,
        effective_status=effective_status(campaign),
        campaign_token=campaign.campaign_token,
        qr_path=qr_path(campaign),
        qr_url=qr_url(campaign, link_base),
        created_at=campaign.created_at,
        updated_at=campaign.updated_at,
    )


# ---------- Public QR resolution ----------

async def resolve_token(db: AsyncSession, raw_token: str) -> tuple[Campaign, Event]:
    """Resolve an opaque campaign QR token. Read-only: never consumes capacity."""
    try:
        token = UUID(raw_token.strip())
    except (ValueError, AttributeError) as exc:
        raise ApiError(404, "CAMPAIGN_NOT_FOUND", "Campaign not found") from exc
    campaign = await repo.get_campaign_by_token(db, token)
    if campaign is None:
        raise ApiError(404, "CAMPAIGN_NOT_FOUND", "Campaign not found")
    event = await repo.get_event_by_id(db, campaign.event_id)
    if event is None or event.status != EventStatus.PUBLISHED:
        raise ApiError(404, "CAMPAIGN_NOT_FOUND", "Campaign not found")
    ensure_usable(campaign)
    return campaign, event


# ---------- Analytics ----------

async def campaign_analytics(db: AsyncSession, campaign: Campaign) -> CampaignAnalyticsResponse:
    """Aggregate campaign performance from the order/payment lifecycle.

    * successful_redemptions / discount_total: REDEEMED rows only (paid or
      free-confirmed orders that have not been cancelled/refunded).
    * attributed_*: NET — orders currently PAID/CONFIRMED.
    * gross_attributed_*: every order that was ever paid/confirmed
      (``paid_at`` set), including ones later cancelled or refunded.
    * reversed_*: gross minus net — paid orders later cancelled/refunded.
      Refunds therefore reduce net revenue but not gross revenue.
    Pending, expired and failed checkouts are never counted as sales.
    """
    redemption_rows = (
        await db.execute(
            select(
                PromoRedemption.status,
                func.count(PromoRedemption.id),
                func.coalesce(func.sum(PromoRedemption.discount_amount), 0),
            )
            .where(PromoRedemption.campaign_id == campaign.id)
            .group_by(PromoRedemption.status)
        )
    ).all()
    counts = {status: (int(count), Decimal(total)) for status, count, total in redemption_rows}

    committed = Order.status.in_(_COMMITTED_ORDER_STATUSES)
    was_paid = Order.paid_at.is_not(None)
    reversed_ = and_(was_paid, Order.status.in_(_REVERSED_ORDER_STATUSES))
    zero = Decimal("0.00")
    row = (
        await db.execute(
            select(
                func.coalesce(func.sum(case((committed, 1), else_=0)), 0),
                func.coalesce(func.sum(case((committed, Order.total_amount), else_=zero)), 0),
                func.coalesce(func.sum(case((was_paid, 1), else_=0)), 0),
                func.coalesce(func.sum(case((was_paid, Order.total_amount), else_=zero)), 0),
                func.coalesce(func.sum(case((reversed_, 1), else_=0)), 0),
                func.coalesce(func.sum(case((reversed_, Order.total_amount), else_=zero)), 0),
            ).where(Order.campaign_id == campaign.id)
        )
    ).one()

    def money(value: Any) -> Decimal:
        return Decimal(value).quantize(CENT)

    return CampaignAnalyticsResponse(
        campaign_id=campaign.id,
        promo_code=campaign.promo_code,
        currency="KZT",
        redemption_limit=campaign.max_redemptions,
        redemption_count=campaign.redemption_count,
        reserved_redemptions=counts.get("RESERVED", (0, zero))[0],
        successful_redemptions=counts.get("REDEEMED", (0, zero))[0],
        released_redemptions=counts.get("RELEASED", (0, zero))[0],
        discount_total=money(counts.get("REDEEMED", (0, zero))[1]),
        attributed_order_count=int(row[0]),
        attributed_revenue=money(row[1]),
        gross_attributed_order_count=int(row[2]),
        gross_attributed_revenue=money(row[3]),
        reversed_order_count=int(row[4]),
        reversed_revenue=money(row[5]),
    )
