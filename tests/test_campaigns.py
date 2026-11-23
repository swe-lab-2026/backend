"""SCRUM-15: promo-code campaigns, discount pricing, redemption accounting,
campaign QR separation and analytics."""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from app.core.errors import ApiError
from app.db.models import (
    AuditLog,
    Campaign,
    DiscountType,
    Order,
    OrderItem,
    OrderStatus,
    PromoRedemption,
)
from app.services import campaigns as campaign_service
from app.services import orders as order_service
from tests.conftest import API, access_token_for, bearer, create_user
from tests.test_orders import _activate_paid_sales, _make_event, _ticket_payload
from tests.test_paid_sales import _create_owner

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
needs_pg = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="row-lock concurrency test requires PostgreSQL"
)


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


# ---------- Fixtures / helpers ----------

async def _setup(client, app, db, *, prefix="camp", paid=True):
    """Published event with GA (25.00) and VIP (100.00) ticket types."""
    owner, owner_token = await _create_owner(client, app, db, f"{prefix}-owner@example.com")
    event, ga = await _make_event(client, owner_token)
    vip_response = await client.post(
        f"{API}/events/{event['id']}/ticket-types",
        json=_ticket_payload(name="VIP", price="100.00", capacity=10),
        headers=bearer(owner_token),
    )
    assert vip_response.status_code == 201, vip_response.text
    if paid:
        await _activate_paid_sales(client, db, owner, owner_token, event["id"])
    return owner, owner_token, event, ga, vip_response.json()


async def _campaign(client, owner_token, event_id, **overrides):
    payload = {
        "name": "Spring promo",
        "code": "spring10",
        "discount_type": "PERCENT",
        "discount_value": "10",
    }
    payload.update(overrides)
    return await client.post(
        f"{API}/events/{event_id}/campaigns", json=payload, headers=bearer(owner_token)
    )


async def _buyer(app, db, email):
    user = await create_user(db, email=email)
    return user, access_token_for(app, user)


async def _order(client, token, event_id, items, **extra):
    return await client.post(
        f"{API}/orders",
        json={"event_id": event_id, "items": items, **extra},
        headers=bearer(token),
    )


def _item(ticket_type, quantity=1):
    return {"ticket_type_id": ticket_type["id"], "quantity": quantity}


async def _pay(client, token, order_id, outcome="success"):
    start = await client.post(
        f"{API}/orders/{order_id}/payments/simulate", headers=bearer(token)
    )
    assert start.status_code == 201, start.text
    result = await client.post(
        f"{API}/orders/payments/{start.json()['payment_id']}/simulate-result",
        json={"outcome": outcome},
        headers=bearer(token),
    )
    assert result.status_code == 200, result.text
    return result.json()


async def _campaign_row(db, campaign_id) -> Campaign:
    db.expire_all()
    campaign = await db.get(Campaign, UUID(campaign_id))
    assert campaign is not None
    return campaign


async def _analytics(client, owner_token, event_id, campaign_id):
    response = await client.get(
        f"{API}/events/{event_id}/campaigns/{campaign_id}/analytics",
        headers=bearer(owner_token),
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------- Campaign management ----------

async def test_create_percentage_campaign(client, app, db):
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="pct")
    response = await _campaign(
        client, owner_token, event["id"], ticket_type_ids=[ga["id"]], redemption_limit=50
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["code"] == "SPRING10"  # normalized, case-insensitive
    assert body["discount_type"] == "PERCENT"
    assert body["discount_value"] == "10.00"
    assert body["ticket_type_ids"] == [ga["id"]]
    assert body["redemption_limit"] == 50
    assert body["redemption_count"] == 0
    assert body["remaining_redemptions"] == 50
    assert body["is_active"] is True
    assert body["effective_status"] == "ACTIVE"
    assert body["qr_path"] == f"/campaigns/{body['campaign_token']}"
    # No public web origin configured -> no invented domain.
    assert body["qr_url"] is None

    audit = await db.scalar(
        select(AuditLog).where(
            AuditLog.action == "campaign.created", AuditLog.entity_id == UUID(body["id"])
        )
    )
    assert audit is not None and audit.after_data["code"] == "SPRING10"

    listed = await client.get(
        f"{API}/events/{event['id']}/campaigns", headers=bearer(owner_token)
    )
    assert [row["id"] for row in listed.json()] == [body["id"]]


async def test_create_fixed_kzt_campaign(client, app, db):
    _owner, owner_token, event, _ga, _vip = await _setup(client, app, db, prefix="fixed")
    response = await _campaign(
        client, owner_token, event["id"], code="MINUS500",
        discount_type="FIXED", discount_value="500.00",
        starts_at=_iso(timedelta(days=-1)), expires_at=_iso(timedelta(days=5)),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["discount_type"] == "FIXED"
    assert body["discount_value"] == "500.00"
    assert body["ticket_type_ids"] == []  # applies to all ticket types


@pytest.mark.parametrize(
    "overrides",
    [
        {"discount_type": "PERCENT", "discount_value": "0"},
        {"discount_type": "PERCENT", "discount_value": "100.01"},
        {"discount_type": "PERCENT", "discount_value": "-5"},
        {"discount_type": "FIXED", "discount_value": "0"},
        {"discount_type": "FIXED", "discount_value": "-10"},
        {"discount_type": "FIXED", "discount_value": "1.234"},
        {"discount_type": "BOGO", "discount_value": "5"},
        {"redemption_limit": 0},
        {"code": "a"},
        {"code": "bad code!"},
        {"starts_at": "2030-01-02T00:00:00+00:00", "expires_at": "2030-01-01T00:00:00+00:00"},
        {"starts_at": "2030-01-01T00:00:00"},  # naive timestamp
    ],
)
async def test_invalid_campaign_payloads_are_rejected(client, app, db, overrides):
    _owner, owner_token, event, _ga, _vip = await _setup(client, app, db, prefix="invalid")
    response = await _campaign(client, owner_token, event["id"], **overrides)
    assert response.status_code == 422, response.text
    assert await db.scalar(select(func.count()).select_from(Campaign)) == 0


async def test_campaign_ticket_types_must_belong_to_event(client, app, db):
    _owner, owner_token, event, _ga, _vip = await _setup(client, app, db, prefix="foreign")
    other_event, other_type = await _make_event(client, owner_token, title="Other event")
    response = await _campaign(
        client, owner_token, event["id"], ticket_type_ids=[other_type["id"]]
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_TICKET_TYPE"
    del other_event


async def test_duplicate_promo_code_is_rejected_case_insensitively(client, app, db):
    _owner, owner_token, event, _ga, _vip = await _setup(client, app, db, prefix="dup")
    assert (await _campaign(client, owner_token, event["id"], code="SAVE")).status_code == 201
    duplicate = await _campaign(client, owner_token, event["id"], code="save")
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "PROMO_CODE_TAKEN"


async def test_unauthorized_campaign_management_is_rejected(client, app, db):
    _owner, owner_token, event, _ga, _vip = await _setup(client, app, db, prefix="authz")
    campaign = (await _campaign(client, owner_token, event["id"])).json()
    _other, other_token = await _create_owner(client, app, db, "authz-other@example.com")
    _buyer_user, buyer_token = await _buyer(app, db, "authz-buyer@example.com")
    base = f"{API}/events/{event['id']}/campaigns"

    for token in (other_token, buyer_token):
        requests = [
            client.post(base, json={"name": "x", "code": "HACK", "discount_type": "PERCENT",
                                    "discount_value": "50"}, headers=bearer(token)),
            client.get(base, headers=bearer(token)),
            client.get(f"{base}/{campaign['id']}", headers=bearer(token)),
            client.patch(f"{base}/{campaign['id']}", json={"is_active": False},
                         headers=bearer(token)),
            client.get(f"{base}/{campaign['id']}/analytics", headers=bearer(token)),
            client.get(f"{base}/analytics", headers=bearer(token)),
            client.get(f"{base}/{campaign['id']}/qr.png", headers=bearer(token)),
        ]
        for response in await asyncio.gather(*requests):
            assert response.status_code == 403, response.text
    assert (await client.get(base)).status_code == 401

    row = await _campaign_row(db, campaign["id"])
    assert row.status.value == "ACTIVE"
    assert await db.scalar(select(func.count()).select_from(Campaign)) == 1


async def test_update_disable_and_guardrails(client, app, db):
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="upd")
    campaign = (await _campaign(client, owner_token, event["id"], redemption_limit=5)).json()
    url = f"{API}/events/{event['id']}/campaigns/{campaign['id']}"

    renamed = await client.patch(url, json={"name": "Renamed", "redemption_limit": 3},
                                 headers=bearer(owner_token))
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Renamed"
    assert renamed.json()["redemption_limit"] == 3

    bad_window = await client.patch(
        url, json={"starts_at": _iso(timedelta(days=3)), "expires_at": _iso(timedelta(days=1))},
        headers=bearer(owner_token),
    )
    assert bad_window.status_code == 400

    _buyer_user, buyer_token = await _buyer(app, db, "upd-buyer@example.com")
    order = await _order(client, buyer_token, event["id"], [_item(ga)], promo_code="SPRING10")
    assert order.status_code == 201, order.text

    locked = await client.patch(url, json={"discount_value": "90"}, headers=bearer(owner_token))
    assert locked.status_code == 409
    assert locked.json()["error"]["code"] == "CAMPAIGN_IN_USE"

    below = await client.patch(url, json={"redemption_limit": 1}, headers=bearer(owner_token))
    assert below.status_code == 200  # equal to usage is fine
    _second, second_token = await _buyer(app, db, "upd-buyer-2@example.com")
    exhausted = await _order(client, second_token, event["id"], [_item(ga)], promo_code="spring10")
    assert exhausted.json()["error"]["code"] == "PROMO_LIMIT_REACHED"

    disabled = await client.patch(url, json={"is_active": False}, headers=bearer(owner_token))
    assert disabled.status_code == 200
    assert disabled.json()["effective_status"] == "DISABLED"

    actions = (
        await db.execute(
            select(AuditLog.action).where(AuditLog.entity_id == UUID(campaign["id"]))
        )
    ).scalars().all()
    assert actions.count("campaign.updated") >= 3


# ---------- Validation / preview ----------

async def _validate(client, token, event_id, items, **source):
    return await client.post(
        f"{API}/campaigns/validate",
        json={"event_id": event_id, "items": items, **source},
        headers=bearer(token),
    )


async def test_promo_validation_errors(client, app, db):
    _owner, owner_token, event, ga, vip = await _setup(client, app, db, prefix="val")
    await _campaign(client, owner_token, event["id"], code="EXPIRED",
                    starts_at=_iso(timedelta(days=-3)), expires_at=_iso(timedelta(days=-1)))
    await _campaign(client, owner_token, event["id"], code="LATER",
                    starts_at=_iso(timedelta(days=1)))
    await _campaign(client, owner_token, event["id"], code="OFF", is_active=False)
    await _campaign(client, owner_token, event["id"], code="VIPONLY",
                    ticket_type_ids=[vip["id"]])
    _user, token = await _buyer(app, db, "val-buyer@example.com")

    cases = {
        "NOPE": (404, "PROMO_NOT_FOUND"),
        "expired": (409, "PROMO_EXPIRED"),
        "LATER": (409, "PROMO_NOT_STARTED"),
        "OFF": (409, "PROMO_DISABLED"),
        "VIPONLY": (409, "PROMO_NOT_APPLICABLE"),
    }
    for code, (status, error_code) in cases.items():
        response = await _validate(client, token, event["id"], [_item(ga)], code=code)
        assert response.status_code == status, (code, response.text)
        assert response.json()["error"]["code"] == error_code
        # Same errors are enforced at checkout, not only in the preview.
        checkout = await _order(client, token, event["id"], [_item(ga)], promo_code=code)
        assert checkout.status_code == status, (code, checkout.text)
        assert checkout.json()["error"]["code"] == error_code

    assert await db.scalar(select(func.count()).select_from(Order)) == 0
    assert (await client.post(f"{API}/campaigns/validate", json={})).status_code == 401


async def test_exhausted_campaign_is_rejected(client, app, db):
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="exh")
    campaign = (await _campaign(client, owner_token, event["id"], redemption_limit=1)).json()
    _first, first_token = await _buyer(app, db, "exh-1@example.com")
    first = await _order(client, first_token, event["id"], [_item(ga)], promo_code="SPRING10")
    assert first.status_code == 201
    await _pay(client, first_token, first.json()["id"])

    _second, second_token = await _buyer(app, db, "exh-2@example.com")
    for response in (
        await _validate(client, second_token, event["id"], [_item(ga)], code="SPRING10"),
        await _order(client, second_token, event["id"], [_item(ga)], promo_code="SPRING10"),
    ):
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "PROMO_LIMIT_REACHED"
    assert (await _campaign_row(db, campaign["id"])).redemption_count == 1


async def test_preview_does_not_consume_redemptions(client, app, db):
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="prev")
    campaign = (await _campaign(client, owner_token, event["id"], redemption_limit=1)).json()
    _user, token = await _buyer(app, db, "prev-buyer@example.com")
    for _ in range(5):
        response = await _validate(client, token, event["id"], [_item(ga, 2)], code="spring10")
        assert response.status_code == 200, response.text
    body = response.json()
    assert body["subtotal"] == "50.00"
    assert body["discount_amount"] == "5.00"
    assert body["fee_amount"] == "1.35"
    assert body["final_total"] == "46.35"
    assert (await _campaign_row(db, campaign["id"])).redemption_count == 0
    assert await db.scalar(select(func.count()).select_from(PromoRedemption)) == 0


# ---------- Discount calculation ----------

async def test_checkout_total_is_server_calculated(client, app, db):
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="total")
    await _campaign(client, owner_token, event["id"])
    _user, token = await _buyer(app, db, "total-buyer@example.com")
    response = await _order(
        client, token, event["id"], [_item(ga, 2)], promo_code="SPRING10",
        # Client-supplied money fields must be ignored.
        total_amount="0.01", discount_amount="50.00", subtotal_amount="0.01",
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["subtotal_amount"] == "50.00"
    assert body["discount_amount"] == "5.00"
    assert body["fee_amount"] == "1.35"  # 3% of discounted 45.00
    assert body["total_amount"] == "46.35"
    assert body["status"] == "PENDING"
    assert body["items"][0]["final_price"] == "22.50"

    payment = await _pay(client, token, body["id"])
    assert payment["amount"] == "46.35"
    assert payment["status"] == "SUCCEEDED"


async def test_mixed_ticket_types_only_discount_eligible_units(client, app, db):
    _owner, owner_token, event, ga, vip = await _setup(client, app, db, prefix="mixed")
    await _campaign(client, owner_token, event["id"], code="VIP25", discount_value="25",
                    ticket_type_ids=[vip["id"]])
    _user, token = await _buyer(app, db, "mixed-buyer@example.com")
    items = [_item(ga, 2), _item(vip, 1)]

    preview = await _validate(client, token, event["id"], items, code="VIP25")
    assert preview.status_code == 200, preview.text
    lines = {line["ticket_type_id"]: line for line in preview.json()["lines"]}
    assert lines[ga["id"]]["eligible"] is False
    assert lines[ga["id"]]["discount_amount"] == "0.00"
    assert lines[vip["id"]]["eligible"] is True
    assert lines[vip["id"]]["discount_amount"] == "25.00"
    assert preview.json()["eligible_subtotal"] == "100.00"

    order = await _order(client, token, event["id"], items, promo_code="VIP25")
    body = order.json()
    assert body["subtotal_amount"] == "150.00"
    assert body["discount_amount"] == "25.00"
    assert body["total_amount"] == preview.json()["final_total"] == "128.75"
    rows = (
        await db.execute(select(OrderItem).where(OrderItem.order_id == UUID(body["id"])))
    ).scalars().all()
    by_type = sorted((str(row.ticket_type_id), row.final_price) for row in rows)
    assert sorted(price for _tt, price in by_type) == [
        Decimal("25.00"), Decimal("25.00"), Decimal("75.00")
    ]


async def test_fixed_discount_is_capped_and_can_make_order_free(client, app, db):
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="cap")
    campaign = (await _campaign(client, owner_token, event["id"], code="BIG",
                                discount_type="FIXED", discount_value="1000")).json()
    _user, token = await _buyer(app, db, "cap-buyer@example.com")
    response = await _order(client, token, event["id"], [_item(ga, 2)], promo_code="BIG")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["discount_amount"] == "50.00"  # capped at the eligible subtotal
    assert body["total_amount"] == "0.00"
    assert body["fee_amount"] == "0.00"
    assert body["status"] == "PAID"  # nothing to pay -> confirmed immediately
    assert len(body["tickets"]) == 2
    redemption = await db.scalar(
        select(PromoRedemption).where(PromoRedemption.order_id == UUID(body["id"]))
    )
    assert redemption is not None and redemption.status == "REDEEMED"
    assert (await _campaign_row(db, campaign["id"])).redemption_count == 1


def test_fixed_discount_allocation_is_deterministic():
    tt_a, tt_b = UUID(int=1), UUID(int=2)
    lines = [
        campaign_service.PriceLine(tt_b, Decimal("10.00"), 2),
        campaign_service.PriceLine(tt_a, Decimal("33.33"), 1),
    ]
    results = [
        campaign_service.calculate_discount(
            discount_type=DiscountType.FIXED, discount_value=Decimal("10.00"),
            lines=list(reversed(lines)) if flip else lines, applicable=None, currency="KZT",
        )
        for flip in (False, True)
    ]
    assert results[0].per_unit == results[1].per_unit
    assert results[0].total == Decimal("10.00")
    for ticket_type_id, amounts in results[0].per_unit.items():
        price = Decimal("33.33") if ticket_type_id == tt_a else Decimal("10.00")
        assert all(Decimal("0") <= amount <= price for amount in amounts)

    percent = campaign_service.calculate_discount(
        discount_type=DiscountType.PERCENT, discount_value=Decimal("12.5"),
        lines=[campaign_service.PriceLine(tt_a, Decimal("0.99"), 3)],
        applicable=None, currency="KZT",
    )
    # 0.99 * 12.5% = 0.12375 -> 0.12 per unit (half-up at the tiyn).
    assert percent.per_unit[tt_a] == [Decimal("0.12")] * 3
    assert percent.total == Decimal("0.36")

    with pytest.raises(ApiError) as exc:
        campaign_service.calculate_discount(
            discount_type=DiscountType.FIXED, discount_value=Decimal("5"),
            lines=[campaign_service.PriceLine(tt_a, Decimal("5"), 1)],
            applicable=None, currency="USD",
        )
    assert exc.value.code == "PROMO_NOT_APPLICABLE"


async def test_legacy_purchase_endpoint_applies_promo(client, app, db):
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="legacy")
    await _campaign(client, owner_token, event["id"], code="HALF", discount_value="50")
    _user, token = await _buyer(app, db, "legacy-buyer@example.com")
    response = await client.post(
        f"{API}/events/{event['id']}/ticket-types/{ga['id']}/purchase",
        json={"quantity": 1, "promo_code": "half"},
        headers=bearer(token),
    )
    assert response.status_code == 201, response.text
    assert response.json()["total_amount"] == "12.88"  # 12.50 + 3% (0.375 -> 0.38)


# ---------- Redemption lifecycle ----------

async def test_failed_payment_releases_reserved_redemption(client, app, db):
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="fail")
    campaign = (await _campaign(client, owner_token, event["id"], redemption_limit=1)).json()
    _first, first_token = await _buyer(app, db, "fail-1@example.com")
    _second, second_token = await _buyer(app, db, "fail-2@example.com")

    pending = await _order(client, first_token, event["id"], [_item(ga)], promo_code="SPRING10")
    assert pending.status_code == 201
    assert (await _campaign_row(db, campaign["id"])).redemption_count == 1
    blocked = await _order(client, second_token, event["id"], [_item(ga)], promo_code="SPRING10")
    assert blocked.json()["error"]["code"] == "PROMO_LIMIT_REACHED"

    failed = await _pay(client, first_token, pending.json()["id"], outcome="failed")
    assert failed["status"] == "FAILED"
    assert (await _campaign_row(db, campaign["id"])).redemption_count == 0
    released = await db.scalar(
        select(PromoRedemption).where(PromoRedemption.order_id == UUID(pending.json()["id"]))
    )
    assert released is not None and released.status == "RELEASED"
    assert released.released_at is not None

    retry = await _order(client, second_token, event["id"], [_item(ga)], promo_code="SPRING10")
    assert retry.status_code == 201, retry.text

    analytics = await _analytics(client, owner_token, event["id"], campaign["id"])
    assert analytics["successful_redemptions"] == 0
    assert analytics["reserved_redemptions"] == 1
    assert analytics["attributed_order_count"] == 0  # failed checkout is not a sale


async def test_expired_checkout_releases_reserved_redemption(client, app, db):
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="expire")
    campaign = (await _campaign(client, owner_token, event["id"], redemption_limit=1)).json()
    _user, token = await _buyer(app, db, "expire-buyer@example.com")
    pending = await _order(client, token, event["id"], [_item(ga)], promo_code="SPRING10")
    order_id = UUID(pending.json()["id"])

    await db.execute(
        update(Order).where(Order.id == order_id)
        .values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    )
    await db.commit()
    await order_service.expire_holds(db)
    await db.commit()

    assert (await _campaign_row(db, campaign["id"])).redemption_count == 0
    assert (await db.get(Order, order_id)).status == OrderStatus.EXPIRED
    # Re-running cleanup is idempotent: no double release / negative count.
    await order_service.expire_holds(db)
    await db.commit()
    assert (await _campaign_row(db, campaign["id"])).redemption_count == 0


async def test_redemption_is_idempotent_per_checkout(client, app, db):
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="idem")
    campaign = (await _campaign(client, owner_token, event["id"], redemption_limit=10)).json()
    _user, token = await _buyer(app, db, "idem-buyer@example.com")
    first = await _order(client, token, event["id"], [_item(ga)], promo_code="SPRING10")
    assert first.status_code == 201

    repeat = await _order(client, token, event["id"], [_item(ga)], promo_code="SPRING10")
    assert repeat.status_code == 409
    assert repeat.json()["error"]["code"] == "PROMO_ALREADY_RESERVED"
    assert repeat.json()["error"]["details"]["order_id"] == first.json()["id"]

    start = await client.post(
        f"{API}/orders/{first.json()['id']}/payments/simulate", headers=bearer(token)
    )
    payment_id = start.json()["payment_id"]
    for _ in range(3):
        resolved = await client.post(
            f"{API}/orders/payments/{payment_id}/simulate-result",
            json={"outcome": "success"}, headers=bearer(token),
        )
        assert resolved.status_code == 200
    row = await _campaign_row(db, campaign["id"])
    assert row.redemption_count == 1
    redemptions = (
        await db.execute(select(PromoRedemption.status).where(
            PromoRedemption.campaign_id == UUID(campaign["id"])))
    ).scalars().all()
    assert redemptions == ["REDEEMED"]


async def test_campaign_attribution_persists_after_disable(client, app, db):
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="attr")
    campaign = (await _campaign(client, owner_token, event["id"])).json()
    _user, token = await _buyer(app, db, "attr-buyer@example.com")
    order = (await _order(client, token, event["id"], [_item(ga)], promo_code="spring10")).json()
    await _pay(client, token, order["id"])

    await client.patch(
        f"{API}/events/{event['id']}/campaigns/{campaign['id']}",
        json={"is_active": False, "name": "Archived"}, headers=bearer(owner_token),
    )
    stored = await client.get(f"{API}/orders/{order['id']}", headers=bearer(token))
    body = stored.json()
    assert body["campaign_id"] == campaign["id"]
    assert body["promo_code"] == "SPRING10"
    assert body["subtotal_amount"] == "25.00"
    assert body["discount_amount"] == "2.50"
    assert body["total_amount"] == "23.18"

    analytics = await _analytics(client, owner_token, event["id"], campaign["id"])
    assert analytics["attributed_order_count"] == 1


# ---------- Campaign QR ----------

async def test_campaign_qr_resolution_and_checkout(client, app, db):
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="qr")
    campaign = (await _campaign(client, owner_token, event["id"], redemption_limit=3)).json()
    token = campaign["campaign_token"]

    for _ in range(3):  # repeated scans are read-only
        resolved = await client.get(f"{API}/campaigns/qr/{token}")
        assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    assert body["event"]["id"] == event["id"]
    assert body["promotion"]["discount_type"] == "PERCENT"
    assert body["checkout"] == {"event_id": event["id"], "campaign_token": token}
    # Minimal public surface: no code, counts, limits or internal ids.
    flat = str(body)
    assert "SPRING10" not in flat and "redemption" not in flat and campaign["id"] not in flat
    assert (await _campaign_row(db, campaign["id"])).redemption_count == 0

    _user, buyer_token = await _buyer(app, db, "qr-buyer@example.com")
    order = await _order(client, buyer_token, event["id"], [_item(ga)], campaign_token=token)
    assert order.status_code == 201, order.text
    assert order.json()["campaign_id"] == campaign["id"]
    assert order.json()["discount_amount"] == "2.50"

    both = await _order(client, buyer_token, event["id"], [_item(ga)],
                        campaign_token=token, promo_code="SPRING10")
    assert both.status_code == 422


async def test_invalid_and_inactive_campaign_qr_tokens(client, app, db):
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="badqr")
    other_event, _other_type = await _make_event(client, owner_token, title="Other")
    off = (await _campaign(client, owner_token, event["id"], code="OFF", is_active=False)).json()
    old = (await _campaign(client, owner_token, event["id"], code="OLD",
                           starts_at=_iso(timedelta(days=-3)),
                           expires_at=_iso(timedelta(days=-1)))).json()
    live = (await _campaign(client, owner_token, event["id"], code="LIVE")).json()

    for raw in (str(uuid4()), "not-a-token", "%27%20OR%201=1--", "0" * 36):
        response = await client.get(f"{API}/campaigns/qr/{raw}")
        assert response.status_code == 404, raw
        assert response.json()["error"]["code"] == "CAMPAIGN_NOT_FOUND"
    assert (await client.get(f"{API}/campaigns/qr/{off['campaign_token']}")).json()[
        "error"]["code"] == "PROMO_DISABLED"
    assert (await client.get(f"{API}/campaigns/qr/{old['campaign_token']}")).json()[
        "error"]["code"] == "PROMO_EXPIRED"

    # Free ticket on the other event so paid-sales activation is irrelevant.
    await client.patch(
        f"{API}/events/{other_event['id']}/ticket-types/{_other_type['id']}",
        json={"price": "0.00"}, headers=bearer(owner_token),
    )
    _user, buyer_token = await _buyer(app, db, "badqr-buyer@example.com")
    wrong_event = await _order(client, buyer_token, other_event["id"],
                               [{"ticket_type_id": _other_type["id"], "quantity": 1}],
                               campaign_token=live["campaign_token"])
    assert wrong_event.status_code == 409
    assert wrong_event.json()["error"]["code"] == "PROMO_NOT_APPLICABLE"
    del ga


async def test_campaign_qr_is_rejected_by_admission_endpoints(client, app, db):
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="sep")
    campaign = (await _campaign(client, owner_token, event["id"])).json()
    token = campaign["campaign_token"]
    _user, buyer_token = await _buyer(app, db, "sep-buyer@example.com")
    order = (await _order(client, buyer_token, event["id"], [_item(ga)])).json()
    await _pay(client, buyer_token, order["id"])
    paid = (await client.get(f"{API}/orders/{order['id']}", headers=bearer(buyer_token))).json()
    ticket_qr = paid["tickets"][0]["qr_payload"]

    for payload in (token, f"https://web.example.test/campaigns/{token}",
                    f"https://web.example.test/campaigns/{token}/"):
        for path in ("tickets/validate", "check-in"):
            response = await client.post(
                f"{API}/events/{event['id']}/{path}", json={"qr_payload": payload},
                headers=bearer(owner_token),
            )
            assert response.status_code == 400, (payload, response.text)
            assert response.json()["error"]["code"] == "CAMPAIGN_QR_NOT_ADMISSION"

    for payload in (f"https://web.example.test/campaigns/{uuid4()}", "https://evil.test/x",
                    "v1.garbage.sig", "random-junk"):
        response = await client.post(
            f"{API}/events/{event['id']}/check-in", json={"qr_payload": payload},
            headers=bearer(owner_token),
        )
        assert response.status_code == 401, (payload, response.text)
        assert response.json()["error"]["code"] == "INVALID_TICKET_QR"

    # The ticket QR is not a campaign token either.
    assert (await client.get(f"{API}/campaigns/qr/{ticket_qr}")).status_code == 404
    # The real ticket still checks in.
    ok = await client.post(f"{API}/events/{event['id']}/check-in",
                           json={"qr_payload": ticket_qr}, headers=bearer(owner_token))
    assert ok.status_code == 200, ok.text
    assert (await _campaign_row(db, campaign["id"])).redemption_count == 0


async def test_campaign_qr_png_requires_configured_https_origin(client, app, db):
    _owner, owner_token, event, _ga, _vip = await _setup(client, app, db, prefix="png")
    campaign = (await _campaign(client, owner_token, event["id"])).json()
    url = f"{API}/events/{event['id']}/campaigns/{campaign['id']}/qr.png"
    missing = await client.get(url, headers=bearer(owner_token))
    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == "CAMPAIGN_LINK_NOT_CONFIGURED"

    app.state.settings.public_web_base_url = "https://web.example.test"
    try:
        detail = await client.get(
            f"{API}/events/{event['id']}/campaigns/{campaign['id']}", headers=bearer(owner_token)
        )
        assert detail.json()["qr_url"] == (
            f"https://web.example.test/campaigns/{campaign['campaign_token']}"
        )
        png = await client.get(url, headers=bearer(owner_token))
        assert png.status_code == 200
        assert png.headers["content-type"] == "image/png"
        assert png.content.startswith(b"\x89PNG")
    finally:
        app.state.settings.public_web_base_url = ""


# ---------- Analytics ----------

async def test_campaign_analytics_after_purchase_and_refund(client, app, db):
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="ana")
    campaign = (await _campaign(client, owner_token, event["id"])).json()
    tokens = []
    for index in range(3):
        _user, token = await _buyer(app, db, f"ana-{index}@example.com")
        tokens.append(token)
    orders = [
        (await _order(client, token, event["id"], [_item(ga)], promo_code="SPRING10")).json()
        for token in tokens
    ]
    await _pay(client, tokens[0], orders[0]["id"])
    await _pay(client, tokens[1], orders[1]["id"])
    await _pay(client, tokens[2], orders[2]["id"], outcome="failed")

    analytics = await _analytics(client, owner_token, event["id"], campaign["id"])
    assert analytics["promo_code"] == "SPRING10"
    assert analytics["successful_redemptions"] == 2
    assert analytics["released_redemptions"] == 1
    assert analytics["discount_total"] == "5.00"
    assert analytics["attributed_order_count"] == 2
    assert analytics["attributed_revenue"] == "46.36"  # 2 x 23.18
    assert analytics["gross_attributed_revenue"] == "46.36"
    assert analytics["reversed_order_count"] == 0

    # Refund one paid order (SCRUM-14 customer refund; needs the event policy).
    # Net figures drop, gross keeps the historical sale.
    policy = await client.put(f"{API}/events/{event['id']}/refund-policy",
                              json={"refunds_enabled": True}, headers=bearer(owner_token))
    assert policy.status_code == 200, policy.text
    cancel = await client.post(f"{API}/orders/{orders[1]['id']}/cancel",
                               headers=bearer(tokens[1]))
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["status"] == "REFUNDED"
    analytics = await _analytics(client, owner_token, event["id"], campaign["id"])
    assert analytics["successful_redemptions"] == 1
    assert analytics["discount_total"] == "2.50"
    assert analytics["attributed_order_count"] == 1
    assert analytics["attributed_revenue"] == "23.18"
    assert analytics["gross_attributed_order_count"] == 2
    assert analytics["gross_attributed_revenue"] == "46.36"
    assert analytics["reversed_order_count"] == 1
    assert analytics["reversed_revenue"] == "23.18"
    assert analytics["redemption_count"] == 1

    listed = await client.get(f"{API}/events/{event['id']}/campaigns/analytics",
                              headers=bearer(owner_token))
    assert [row["campaign_id"] for row in listed.json()] == [campaign["id"]]


# ---------- Database-level guarantees ----------

async def test_redemption_limit_is_enforced_by_check_constraint(client, app, db):
    _owner, owner_token, event, _ga, _vip = await _setup(client, app, db, prefix="chk")
    campaign = (await _campaign(client, owner_token, event["id"], redemption_limit=1)).json()
    with pytest.raises(IntegrityError):
        await db.execute(
            text("UPDATE campaigns SET redemption_count = 2 WHERE id = :id"),
            {"id": UUID(campaign["id"]) if TEST_DATABASE_URL else UUID(campaign["id"]).hex},
        )
        await db.commit()
    await db.rollback()


@needs_pg
async def test_concurrent_checkouts_cannot_exceed_last_redemption(client, app, db):
    """Scenario C: one redemption left, many simultaneous buyers."""
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="race")
    campaign = (await _campaign(client, owner_token, event["id"], redemption_limit=3)).json()
    for index in range(2):  # 2 of 3 already used
        _user, token = await _buyer(app, db, f"race-pre-{index}@example.com")
        order = await _order(client, token, event["id"], [_item(ga)], promo_code="SPRING10")
        await _pay(client, token, order.json()["id"])

    tokens = []
    for index in range(6):
        _user, token = await _buyer(app, db, f"race-{index}@example.com")
        tokens.append(token)
    responses = await asyncio.gather(*(
        _order(client, token, event["id"], [_item(ga)], promo_code="SPRING10")
        for token in tokens
    ))
    statuses = sorted(response.status_code for response in responses)
    assert statuses == [201, 409, 409, 409, 409, 409], [r.text for r in responses]
    assert all(
        r.json()["error"]["code"] == "PROMO_LIMIT_REACHED"
        for r in responses if r.status_code == 409
    )
    row = await _campaign_row(db, campaign["id"])
    assert row.redemption_count == 3
    active = await db.scalar(
        select(func.count()).select_from(PromoRedemption).where(
            PromoRedemption.campaign_id == UUID(campaign["id"]),
            PromoRedemption.status.in_(("RESERVED", "REDEEMED")),
        )
    )
    assert active == 3
    winner = next(r for r in responses if r.status_code == 201).json()
    assert winner["total_amount"] == "23.18"


@needs_pg
async def test_reserve_capacity_is_atomic_without_event_lock(client, app, db):
    """The counter itself is race-safe, independent of checkout's event lock."""
    _owner, owner_token, event, _ga, _vip = await _setup(client, app, db, prefix="atomic")
    created = (await _campaign(client, owner_token, event["id"], redemption_limit=1)).json()
    factory = app.state.session_factory
    async with factory() as first, factory() as second:
        campaign_a = await first.get(Campaign, UUID(created["id"]))
        campaign_b = await second.get(Campaign, UUID(created["id"]))
        await campaign_service.reserve_capacity(first, campaign_a)  # holds the row lock
        contender = asyncio.create_task(campaign_service.reserve_capacity(second, campaign_b))
        await asyncio.sleep(0.3)
        assert not contender.done()  # blocked on the row lock
        await first.commit()
        with pytest.raises(ApiError) as exc:
            await contender
        assert exc.value.code == "PROMO_LIMIT_REACHED"
        await second.rollback()
    assert (await _campaign_row(db, created["id"])).redemption_count == 1
