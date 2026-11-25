"""SCRUM-14: refund policy, cancellations, simulated refunds, event
cancellation, lifecycle notifications and the append-only audit log."""

import asyncio
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.db.models import (
    AuditLog,
    Event,
    EventStatus,
    Order,
    OrderItem,
    OrderStatus,
    Payment,
    PaymentStatus,
    Refund,
    Ticket,
    TicketStatus,
    User,
)
from app.services import events as event_service
from app.services import notifications
from app.services import refunds as refund_service
from tests.conftest import API, access_token_for, bearer
from tests.test_campaigns import _buyer, _campaign, _item, _order, _pay, _setup
from tests.test_orders import _event_payload, _make_event, _ticket_payload
from tests.test_paid_sales import _create_owner

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
needs_pg = pytest.mark.skipif(not TEST_DATABASE_URL, reason="requires real PostgreSQL")


# ---------- Helpers ----------

async def _enable_refunds(client, owner_token, event_id, **extra):
    response = await client.put(
        f"{API}/events/{event_id}/refund-policy",
        json={"refunds_enabled": True, **extra},
        headers=bearer(owner_token),
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _paid_order(client, app, db, event, ticket_type, email, **extra):
    _user, token = await _buyer(app, db, email)
    order = await _order(client, token, event["id"], [_item(ticket_type)], **extra)
    assert order.status_code == 201, order.text
    await _pay(client, token, order.json()["id"])
    return token, order.json()


async def _fresh(db, model, key):
    db.expire_all()
    return await db.get(model, UUID(str(key)))


async def _tickets(db, order_id):
    db.expire_all()
    rows = await db.execute(
        select(Ticket).join(OrderItem, OrderItem.id == Ticket.order_item_id)
        .where(OrderItem.order_id == UUID(str(order_id)))
    )
    return list(rows.scalars().all())


async def _actions(db, **where):
    db.expire_all()
    stmt = select(AuditLog.action)
    for key, value in where.items():
        stmt = stmt.where(getattr(AuditLog, key) == value)
    return list((await db.execute(stmt)).scalars().all())


def _provider(app):
    return app.state.email_provider


async def _free_event(client, app, db, prefix, capacity=5):
    _owner, owner_token = await _create_owner(client, app, db, f"{prefix}-owner@example.com")
    event, ticket_type = await _make_event(client, owner_token)
    patched = await client.patch(
        f"{API}/events/{event['id']}/ticket-types/{ticket_type['id']}",
        json={"price": "0.00", "capacity": capacity},
        headers=bearer(owner_token),
    )
    assert patched.status_code == 200, patched.text
    return owner_token, event, patched.json()


# ---------- Refund policy ----------

async def test_refund_policy_management_and_visibility(client, app, db):
    _owner, owner_token, event, _ga, _vip = await _setup(client, app, db, prefix="pol")
    public = await client.get(f"{API}/events/{event['id']}/refund-policy")
    assert public.status_code == 200
    assert public.json()["refunds_enabled"] is False  # safe default

    body = await _enable_refunds(client, owner_token, event["id"], refund_deadline_hours=24,
                                 policy_text="Full refund until 24h before start")
    assert body["refund_deadline_hours"] == 24
    assert body["refund_deadline"] is not None
    assert body["policy_text"] == "Full refund until 24h before start"

    bad = await client.put(f"{API}/events/{event['id']}/refund-policy",
                           json={"refunds_enabled": True, "refund_deadline_hours": -1},
                           headers=bearer(owner_token))
    assert bad.status_code == 422

    _other, other_token = await _create_owner(client, app, db, "pol-other@example.com")
    _b, buyer_token = await _buyer(app, db, "pol-buyer@example.com")
    for token in (other_token, buyer_token):
        denied = await client.put(f"{API}/events/{event['id']}/refund-policy",
                                  json={"refunds_enabled": False}, headers=bearer(token))
        assert denied.status_code == 403

    entries = await _actions(db, action="event.refund_policy_updated")
    assert len(entries) == 1


# ---------- Customer cancellation / refund ----------

async def test_free_registration_cancellation(client, app, db):
    _owner_token, event, ticket_type = await _free_event(client, app, db, "free", capacity=1)
    _user, token = await _buyer(app, db, "free-buyer@example.com")
    order = (await _order(client, token, event["id"], [_item(ticket_type)])).json()
    assert order["status"] == "PAID" and order["total_amount"] == "0.00"

    # Capacity 1 is taken.
    _other, other_token = await _buyer(app, db, "free-other@example.com")
    sold_out = await _order(client, other_token, event["id"], [_item(ticket_type)])
    assert sold_out.status_code == 409

    cancelled = await client.post(f"{API}/orders/{order['id']}/cancel", headers=bearer(token))
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "CANCELLED"
    assert {t.status for t in await _tickets(db, order["id"])} == {TicketStatus.CANCELLED}
    # No artificial monetary refund for a free registration.
    assert await db.scalar(select(func.count()).select_from(Refund)) == 0

    refund = await client.post(f"{API}/orders/{order['id']}/refund", headers=bearer(token))
    assert refund.status_code == 409
    assert refund.json()["error"]["code"] == "ORDER_NOT_PAID"

    # The seat is released.
    retry = await _order(client, other_token, event["id"], [_item(ticket_type)])
    assert retry.status_code == 201, retry.text


async def test_successful_simulated_full_refund(client, app, db):
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="ref")
    await _enable_refunds(client, owner_token, event["id"])
    token, order = await _paid_order(client, app, db, event, ga, "ref-buyer@example.com")
    payment_before = await db.scalar(select(Payment).where(Payment.order_id == UUID(order["id"])))
    payment_id, paid_at = payment_before.id, payment_before.paid_at

    eligibility = await client.get(f"{API}/orders/{order['id']}/refund-eligibility",
                                   headers=bearer(token))
    assert eligibility.json()["refundable"] is True
    assert eligibility.json()["refund_amount"] == "25.75"

    response = await client.post(f"{API}/orders/{order['id']}/refund", headers=bearer(token))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["replayed"] is False
    assert body["order_status"] == "REFUNDED"
    refund = body["refund"]
    assert refund["amount"] == "25.75"  # full amount actually charged, incl. fee
    assert refund["status"] == "SUCCEEDED"
    assert refund["kind"] == "CUSTOMER_REQUEST"
    assert refund["is_simulated"] is True
    assert "DEMO ONLY" in body["notice"]

    payment = await _fresh(db, Payment, payment_id)
    assert payment.status == PaymentStatus.REFUNDED
    # Original financial record preserved.
    assert payment.amount == Decimal("25.75") and payment.paid_at == paid_at
    stored = await _fresh(db, Order, order["id"])
    assert stored.status == OrderStatus.REFUNDED
    assert stored.total_amount == Decimal("25.75") and stored.paid_at is not None
    assert {t.status for t in await _tickets(db, order["id"])} == {TicketStatus.REFUNDED}

    listed = await client.get(f"{API}/orders/{order['id']}/refunds", headers=bearer(token))
    assert [r["id"] for r in listed.json()] == [refund["id"]]

    # Refund-completed notification, sent after commit.
    sent = _provider(app).notifications("REFUND_COMPLETED")
    assert len(sent) == 1
    assert sent[0].email == "ref-buyer@example.com"
    assert sent[0].details["refund_amount"] == "25.75 KZT"
    assert sent[0].event_name == event["title"]

    # Audit trail, consistent with the business change.
    refund_actions = await _actions(db, entity_id=UUID(refund["id"]))
    assert refund_actions.count("refund.requested") == 1
    assert refund_actions.count("refund.completed") == 1
    assert "order.refunded" in await _actions(db, entity_id=UUID(order["id"]))
    assert "payment.state_changed" in await _actions(db, entity_id=payment_id)
    entry = await db.scalar(select(AuditLog).where(AuditLog.action == "refund.completed"))
    assert entry.actor_type == "CUSTOMER" and entry.request_id
    blob = json.dumps([entry.meta, entry.before_data, entry.after_data])
    assert "@" not in blob  # no customer email / PII in the trail


async def test_refund_disabled_by_policy(client, app, db):
    _owner, _owner_token, event, ga, _vip = await _setup(client, app, db, prefix="dis")
    token, order = await _paid_order(client, app, db, event, ga, "dis-buyer@example.com")
    for path in ("refund", "cancel"):
        response = await client.post(f"{API}/orders/{order['id']}/{path}", headers=bearer(token))
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "REFUNDS_DISABLED"
    assert (await _fresh(db, Order, order["id"])).status == OrderStatus.PAID
    assert {t.status for t in await _tickets(db, order["id"])} == {TicketStatus.VALID}
    assert await db.scalar(select(func.count()).select_from(Refund)) == 0
    assert (await _actions(db, action="refund.denied")) == ["refund.denied"] * 2


async def test_refund_deadline_expired(client, app, db):
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="dl")
    # Event starts in ~48h; a 72h deadline has already passed.
    await _enable_refunds(client, owner_token, event["id"], refund_deadline_hours=72)
    token, order = await _paid_order(client, app, db, event, ga, "dl-buyer@example.com")
    response = await client.post(f"{API}/orders/{order['id']}/refund", headers=bearer(token))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REFUND_DEADLINE_PASSED"
    eligibility = (await client.get(f"{API}/orders/{order['id']}/refund-eligibility",
                                    headers=bearer(token))).json()
    assert eligibility["refundable"] is False
    assert eligibility["reason"] == "REFUND_DEADLINE_PASSED"


async def test_cancellation_by_unauthorized_customer(client, app, db):
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="unauth")
    await _enable_refunds(client, owner_token, event["id"])
    _token, order = await _paid_order(client, app, db, event, ga, "unauth-buyer@example.com")
    _intruder, intruder_token = await _buyer(app, db, "unauth-intruder@example.com")
    for path in ("cancel", "refund"):
        response = await client.post(f"{API}/orders/{order['id']}/{path}",
                                     headers=bearer(intruder_token))
        assert response.status_code == 403
    assert (await _fresh(db, Order, order["id"])).status == OrderStatus.PAID
    assert await db.scalar(select(func.count()).select_from(Refund)) == 0


async def test_event_owner_can_refund_and_cancel_orders_of_own_event(client, app, db):
    """SRS 4.9: organizers cancel free registrations and initiate full refunds."""
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="orgref")
    _token, order = await _paid_order(client, app, db, event, ga, "orgref-buyer@example.com")
    _other, other_token = await _create_owner(client, app, db, "orgref-other@example.com")
    # Another organizer is never allowed.
    for path in ("refund", "cancel"):
        response = await client.post(f"{API}/orders/{order['id']}/{path}",
                                     headers=bearer(other_token))
        assert response.status_code == 403, response.text
    other_cancel = await client.post(f"{API}/events/{event['id']}/cancel",
                                     headers=bearer(other_token))
    assert other_cancel.status_code == 403
    assert (await _fresh(db, Order, order["id"])).status == OrderStatus.PAID

    # The owner refunds although customer refunds are disabled by policy.
    eligibility = await client.get(f"{API}/orders/{order['id']}/refund-eligibility",
                                   headers=bearer(owner_token))
    assert eligibility.json()["refundable"] is True
    refunded = await client.post(f"{API}/orders/{order['id']}/refund",
                                 headers=bearer(owner_token))
    assert refunded.status_code == 200, refunded.text
    assert refunded.json()["order_status"] == "REFUNDED"
    assert refunded.json()["refund"]["kind"] == "ORGANIZER_REQUEST"
    assert refunded.json()["refund"]["amount"] == "25.75"  # full amount charged
    assert {t.status for t in await _tickets(db, order["id"])} == {TicketStatus.REFUNDED}
    replay = await client.post(f"{API}/orders/{order['id']}/refund", headers=bearer(owner_token))
    assert replay.json()["replayed"] is True
    assert await db.scalar(select(func.count()).select_from(Refund)) == 1

    # Free registration: the owner cancels it, no monetary refund.
    free_owner_token, free_event, free_type = await _free_event(client, app, db, "orgfree")
    _b, buyer_token = await _buyer(app, db, "orgfree-buyer@example.com")
    free_order = (await _order(client, buyer_token, free_event["id"], [_item(free_type)])).json()
    cancelled = await client.post(f"{API}/orders/{free_order['id']}/cancel",
                                  headers=bearer(free_owner_token))
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "CANCELLED"
    assert await db.scalar(select(func.count()).select_from(Refund)) == 1


async def test_repeated_refund_requests_are_idempotent(client, app, db):
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="rep")
    await _enable_refunds(client, owner_token, event["id"])
    token, order = await _paid_order(client, app, db, event, ga, "rep-buyer@example.com")
    first = (await client.post(f"{API}/orders/{order['id']}/refund", headers=bearer(token))).json()
    for _ in range(3):
        again = await client.post(f"{API}/orders/{order['id']}/refund", headers=bearer(token))
        assert again.status_code == 200
        assert again.json()["replayed"] is True
        assert again.json()["refund"]["id"] == first["refund"]["id"]
        cancel = await client.post(f"{API}/orders/{order['id']}/cancel", headers=bearer(token))
        assert cancel.status_code == 200 and cancel.json()["status"] == "REFUNDED"
    assert await db.scalar(select(func.count()).select_from(Refund)) == 1
    assert len(_provider(app).notifications("REFUND_COMPLETED")) == 1


@needs_pg
async def test_concurrent_refund_requests_pay_out_once(client, app, db):
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="crf")
    await _enable_refunds(client, owner_token, event["id"])
    token, order = await _paid_order(client, app, db, event, ga, "crf-buyer@example.com")
    responses = await asyncio.gather(*(
        client.post(f"{API}/orders/{order['id']}/{'refund' if i % 2 else 'cancel'}",
                    headers=bearer(token))
        for i in range(6)
    ))
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    fresh = [r for r in responses if r.json().get("replayed") is False]
    assert len(fresh) <= 1
    assert await db.scalar(select(func.count()).select_from(Refund)) == 1
    assert (await _fresh(db, Order, order["id"])).status == OrderStatus.REFUNDED
    assert len(_provider(app).notifications("REFUND_COMPLETED")) == 1


async def test_tickets_rejected_after_cancellation_and_refund(client, app, db):
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="adm")
    await _enable_refunds(client, owner_token, event["id"])
    token, order = await _paid_order(client, app, db, event, ga, "adm-buyer@example.com")
    view = (await client.get(f"{API}/orders/{order['id']}", headers=bearer(token))).json()
    qr = view["tickets"][0]["qr_payload"]
    await client.post(f"{API}/orders/{order['id']}/refund", headers=bearer(token))

    validate = await client.post(f"{API}/events/{event['id']}/tickets/validate",
                                 json={"qr_payload": qr}, headers=bearer(owner_token))
    assert validate.json()["valid"] is False and validate.json()["reason"] == "TICKET_REFUNDED"
    check_in = await client.post(f"{API}/events/{event['id']}/check-in",
                                 json={"qr_payload": qr}, headers=bearer(owner_token))
    assert check_in.status_code == 409
    assert check_in.json()["error"]["code"] == "TICKET_NOT_VALID"


async def test_checked_in_ticket_cannot_be_refunded(client, app, db):
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="chk")
    await _enable_refunds(client, owner_token, event["id"])
    token, order = await _paid_order(client, app, db, event, ga, "chk-buyer@example.com")
    view = (await client.get(f"{API}/orders/{order['id']}", headers=bearer(token))).json()
    ok = await client.post(f"{API}/events/{event['id']}/check-in",
                           json={"qr_payload": view["tickets"][0]["qr_payload"]},
                           headers=bearer(owner_token))
    assert ok.status_code == 200
    response = await client.post(f"{API}/orders/{order['id']}/refund", headers=bearer(token))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TICKET_ALREADY_CHECKED_IN"
    assert await db.scalar(select(func.count()).select_from(Refund)) == 0


async def test_discounted_order_refund_equals_amount_paid(client, app, db):
    """Scenario A: promo purchase -> refund of the discounted amount."""
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="promo")
    campaign = (await _campaign(client, owner_token, event["id"], redemption_limit=1)).json()
    await _enable_refunds(client, owner_token, event["id"])
    token, order = await _paid_order(client, app, db, event, ga, "promo-buyer@example.com",
                                     promo_code="SPRING10")
    assert order["subtotal_amount"] == "25.00" and order["discount_amount"] == "2.50"
    refund = (await client.post(f"{API}/orders/{order['id']}/refund",
                                headers=bearer(token))).json()["refund"]
    assert refund["amount"] == "23.18"  # not the undiscounted 25.75

    analytics = (await client.get(
        f"{API}/events/{event['id']}/campaigns/{campaign['id']}/analytics",
        headers=bearer(owner_token))).json()
    assert analytics["successful_redemptions"] == 0
    assert analytics["reversed_order_count"] == 1
    assert analytics["reversed_revenue"] == "23.18"
    assert analytics["redemption_count"] == 0  # capacity returned
    stored = await _fresh(db, Order, order["id"])
    assert stored.campaign_id == UUID(campaign["id"]) and stored.promo_code_snapshot == "SPRING10"


async def test_pending_checkout_cancellation_releases_hold(client, app, db):
    _owner, _owner_token, event, ga, _vip = await _setup(client, app, db, prefix="pend")
    _user, token = await _buyer(app, db, "pend-buyer@example.com")
    order = (await _order(client, token, event["id"], [_item(ga)])).json()
    start = await client.post(f"{API}/orders/{order['id']}/payments/simulate",
                              headers=bearer(token))
    cancelled = await client.post(f"{API}/orders/{order['id']}/cancel", headers=bearer(token))
    assert cancelled.json()["status"] == "CANCELLED"
    payment = await _fresh(db, Payment, start.json()["payment_id"])
    assert payment.status == PaymentStatus.CANCELLED


async def test_failed_operation_rolls_back_everything(app, db, monkeypatch):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="rb")
        await _enable_refunds(client, owner_token, event["id"])
        token, order = await _paid_order(client, app, db, event, ga, "rb-buyer@example.com")

        async def boom(*_args, **_kwargs):
            raise RuntimeError("outbox unavailable")

        monkeypatch.setattr(notifications, "enqueue", boom)
        response = await client.post(f"{API}/orders/{order['id']}/refund", headers=bearer(token))
        assert response.status_code == 500

    assert (await _fresh(db, Order, order["id"])).status == OrderStatus.PAID
    payment = await db.scalar(select(Payment).where(Payment.order_id == UUID(order["id"])))
    assert payment.status == PaymentStatus.SUCCEEDED
    assert await db.scalar(select(func.count()).select_from(Refund)) == 0
    assert {t.status for t in await _tickets(db, order["id"])} == {TicketStatus.VALID}
    assert await _actions(db, action="refund.completed") == []


# ---------- Event cancellation ----------

async def _mixed_event(client, app, db, prefix):
    """Event with a promo-paid order, a plain paid order, a free order and a pending one."""
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix=prefix)
    free_type = (await client.post(
        f"{API}/events/{event['id']}/ticket-types",
        json=_ticket_payload(name="Community", price="0.00", capacity=10),
        headers=bearer(owner_token))).json()
    campaign = (await _campaign(client, owner_token, event["id"])).json()
    _t1, promo_order = await _paid_order(client, app, db, event, ga, f"{prefix}-a@example.com",
                                         promo_code="SPRING10")
    _t2, paid_order = await _paid_order(client, app, db, event, ga, f"{prefix}-b@example.com")
    _u3, t3 = await _buyer(app, db, f"{prefix}-c@example.com")
    free_order = (await _order(client, t3, event["id"], [_item(free_type)])).json()
    _u4, t4 = await _buyer(app, db, f"{prefix}-d@example.com")
    pending_order = (await _order(client, t4, event["id"], [_item(ga)])).json()
    assert pending_order["status"] == "PENDING"
    return owner_token, event, campaign, {
        "promo": promo_order, "paid": paid_order, "free": free_order,
        "pending": pending_order, "pending_token": t4,
    }


async def test_event_cancellation_refunds_invalidates_and_notifies(client, app, db):
    """Scenario B."""
    owner_token, event, campaign, orders = await _mixed_event(client, app, db, "evc")
    response = await client.post(f"{API}/events/{event['id']}/cancel",
                                 json={"reason": "Venue flooded"}, headers=bearer(owner_token))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "CANCELLED"

    # Refund policy is disabled, yet organizer cancellation refunds in full.
    for key, amount in (("promo", "23.18"), ("paid", "25.75")):
        stored = await _fresh(db, Order, orders[key]["id"])
        assert stored.status == OrderStatus.REFUNDED
        refund = await db.scalar(select(Refund).where(Refund.order_id == stored.id))
        assert refund.kind == "EVENT_CANCELLATION" and refund.amount == Decimal(amount)
        assert {t.status for t in await _tickets(db, stored.id)} == {TicketStatus.REFUNDED}
    free = await _fresh(db, Order, orders["free"]["id"])
    assert free.status == OrderStatus.CANCELLED
    assert {t.status for t in await _tickets(db, free.id)} == {TicketStatus.CANCELLED}
    assert await db.scalar(select(func.count()).select_from(Refund)) == 2
    assert (await _fresh(db, Order, orders["pending"]["id"])).status == OrderStatus.CANCELLED

    # Pending checkout can no longer be paid.
    start = await client.post(f"{API}/orders/{orders['pending']['id']}/payments/simulate",
                              headers=bearer(orders["pending_token"]))
    assert start.status_code == 409

    sent = _provider(app).notifications("EVENT_CANCELLED")
    assert sorted(m.email for m in sent) == [
        "evc-a@example.com", "evc-b@example.com", "evc-c@example.com",
    ]
    by_email = {m.email: m for m in sent}
    assert by_email["evc-a@example.com"].details["refund_amount"] == "23.18 KZT"
    assert by_email["evc-a@example.com"].details["reason"] == "Venue flooded"
    assert "refund_amount" not in by_email["evc-c@example.com"].details
    assert _provider(app).notifications("REFUND_COMPLETED") == []  # one email per order

    actions = await _actions(db, event_id=UUID(event["id"]))
    assert actions.count("event.cancelled") == 1
    assert actions.count("refund.completed") == 2
    assert actions.count("order.refunded") == 2
    cancelled_entry = await db.scalar(select(AuditLog).where(AuditLog.action == "event.cancelled"))
    assert cancelled_entry.actor_type == "ORGANIZER"
    assert cancelled_entry.meta == {"reason": "Venue flooded"}

    # Attribution preserved for historical analytics.
    promo = await _fresh(db, Order, orders["promo"]["id"])
    assert promo.campaign_id == UUID(campaign["id"])
    analytics = (await client.get(
        f"{API}/events/{event['id']}/campaigns/{campaign['id']}/analytics",
        headers=bearer(owner_token))).json()
    assert analytics["gross_attributed_order_count"] == 1
    assert analytics["reversed_order_count"] == 1

    summary = (await client.get(f"{API}/events/{event['id']}/cancellation",
                                headers=bearer(owner_token))).json()
    assert summary["complete"] is True and summary["orders_remaining"] == 0
    assert summary["orders_refunded"] == 2
    assert summary["event_cancellation_refund_total"] == "48.93"


async def test_repeated_and_resumed_event_cancellation(client, app, db):
    owner_token, event, _campaign_body, orders = await _mixed_event(client, app, db, "evr")
    event_id = UUID(event["id"])

    # Simulate a crash after the status change and one processed order.
    async with app.state.session_factory() as session:
        owner_event = await session.get(Event, event_id)
        owner = await session.get(User, owner_event.owner_user_id)
        await event_service.cancel_event(session, owner, owner_event)
        await session.commit()
        done = await refund_service.process_event_cancellation(
            session, event_id, actor_user_id=owner.id, actor_type="ORGANIZER",
            batch_size=1, max_batches=1,
        )
    assert done == 1
    summary = (await client.get(f"{API}/events/{event['id']}/cancellation",
                                headers=bearer(owner_token))).json()
    assert summary["complete"] is False and summary["orders_remaining"] == 3

    # Retrying the organizer action resumes the remaining work.
    retry = await client.post(f"{API}/events/{event['id']}/cancel", headers=bearer(owner_token))
    assert retry.status_code == 200
    again = await client.post(f"{API}/events/{event['id']}/cancel", headers=bearer(owner_token))
    assert again.status_code == 200
    # The background sweep is a no-op once everything is closed.
    async with app.state.session_factory() as session:
        assert await refund_service.resume_event_cancellations(session, batch_size=10) == 0

    assert await db.scalar(select(func.count()).select_from(Refund)) == 2
    assert len(_provider(app).notifications("EVENT_CANCELLED")) == 3
    assert (await _actions(db, action="event.cancelled")) == ["event.cancelled"]
    summary = (await client.get(f"{API}/events/{event['id']}/cancellation",
                                headers=bearer(owner_token))).json()
    assert summary["complete"] is True
    assert (await _fresh(db, Event, event["id"])).status == EventStatus.CANCELLED
    del orders


async def test_customer_cancel_on_cancelled_event_follows_event_policy(client, app, db):
    owner_token, event, _campaign_body, orders = await _mixed_event(client, app, db, "evx")
    async with app.state.session_factory() as session:
        owner_event = await session.get(Event, UUID(event["id"]))
        owner = await session.get(User, owner_event.owner_user_id)
        await event_service.cancel_event(session, owner, owner_event)
        await session.commit()
    buyer_token = access_token_for(app, await _buyer_user(db, "evx-b@example.com"))
    response = await client.post(f"{API}/orders/{orders['paid']['id']}/refund",
                                 headers=bearer(buyer_token))
    assert response.status_code == 200, response.text
    assert response.json()["refund"]["kind"] == "EVENT_CANCELLATION"
    del owner_token


async def _buyer_user(db, email):
    from tests.conftest import get_user

    user = await get_user(db, email)
    assert user is not None
    return user


# ---------- Notifications ----------

async def test_payment_failure_notification(client, app, db):
    _owner, _owner_token, event, ga, _vip = await _setup(client, app, db, prefix="pf")
    _user, token = await _buyer(app, db, "pf-buyer@example.com")
    order = (await _order(client, token, event["id"], [_item(ga)])).json()
    start = await client.post(f"{API}/orders/{order['id']}/payments/simulate",
                              headers=bearer(token))
    for _ in range(2):  # repeated callback must not duplicate the email
        await client.post(f"{API}/orders/payments/{start.json()['payment_id']}/simulate-result",
                          json={"outcome": "failed"}, headers=bearer(token))
    sent = _provider(app).notifications("PAYMENT_FAILED")
    assert len(sent) == 1
    assert sent[0].email == "pf-buyer@example.com"
    assert sent[0].order_number == order["order_number"]
    assert sent[0].details["amount"] == "25.75 KZT"
    changed = await db.scalar(select(AuditLog).where(AuditLog.action == "payment.state_changed"))
    assert changed.after_data == {"status": "FAILED"}

    abandoned_order = (await _order(client, token, event["id"], [_item(ga)])).json()
    await _pay(client, token, abandoned_order["id"], outcome="abandoned")
    assert len(_provider(app).notifications("PAYMENT_FAILED")) == 1


async def test_event_update_notification(client, app, db):
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="upd")
    await _paid_order(client, app, db, event, ga, "upd-buyer@example.com")
    url = f"{API}/events/{event['id']}"

    minor = await client.patch(url, json={"description": "Typo fix"}, headers=bearer(owner_token))
    assert minor.status_code == 200
    assert _provider(app).notifications("EVENT_UPDATED") == []

    major = await client.patch(url, json={"venue_name": "New Hall", "title": "Moved Show"},
                               headers=bearer(owner_token))
    assert major.status_code == 200, major.text
    sent = _provider(app).notifications("EVENT_UPDATED")
    assert len(sent) == 1
    assert sent[0].event_name == "Moved Show" and sent[0].venue == "New Hall"
    assert "venue name" in sent[0].details["changed"]

    await client.patch(url, json={"city": "Astana"}, headers=bearer(owner_token))
    assert len(_provider(app).notifications("EVENT_UPDATED")) == 2


def test_notification_templates_escape_and_include_context():
    from app.services.email import NotificationEmail
    from app.services.email_templates import render_notification_email

    content = render_notification_email(NotificationEmail(
        email="x@example.com", kind="REFUND_COMPLETED", order_number=42,
        event_name="<script>Show</script>",
        event_starts_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        venue="Hall", details={"refund_amount": "10.00 KZT"},
    ))
    assert "Refund completed" in content.subject and "#42" in content.subject
    assert "<script>" not in content.html and "&lt;script&gt;" in content.html
    assert "10.00 KZT" in content.text and "2030-01-01 00:00 UTC" in content.text


# ---------- Audit log ----------

async def test_audit_log_access_is_read_only_and_owner_scoped(client, app, db):
    _owner, owner_token, event, _ga, _vip = await _setup(client, app, db, prefix="aud")
    await _enable_refunds(client, owner_token, event["id"])
    url = f"{API}/events/{event['id']}/audit-log"
    listed = await client.get(url, headers=bearer(owner_token))
    assert listed.status_code == 200
    entry = next(e for e in listed.json() if e["action"] == "event.refund_policy_updated")
    assert set(entry) >= {"id", "timestamp", "actor_id", "actor_type", "action",
                          "entity_type", "entity_id", "metadata"}
    assert entry["actor_type"] == "ORGANIZER"
    filtered = await client.get(url, params={"action": "event.refund_policy_updated"},
                                headers=bearer(owner_token))
    assert len(filtered.json()) == 1

    _other, other_token = await _create_owner(client, app, db, "aud-other@example.com")
    _b, buyer_token = await _buyer(app, db, "aud-buyer@example.com")
    for token in (other_token, buyer_token):
        assert (await client.get(url, headers=bearer(token))).status_code == 403
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = await client.request(method, url, headers=bearer(owner_token))
        assert response.status_code == 405, method
        response = await client.request(method, f"{url}/{entry['id']}",
                                        headers=bearer(owner_token))
        assert response.status_code in (404, 405), method

    # ORM-level guard.
    row = await db.get(AuditLog, entry["id"])
    row.action = "tampered"
    with pytest.raises(PermissionError):
        await db.flush()
    await db.rollback()
    await db.delete(await db.get(AuditLog, entry["id"]))
    with pytest.raises(PermissionError):
        await db.flush()
    await db.rollback()


@needs_pg
async def test_audit_log_is_append_only_in_postgres(client, app, db):
    _owner, owner_token, event, _ga, _vip = await _setup(client, app, db, prefix="pgaud")
    await _enable_refunds(client, owner_token, event["id"])
    for statement in (
        "UPDATE audit_logs SET action = 'tampered'",
        "UPDATE audit_logs SET metadata = '{}'::jsonb",
        "DELETE FROM audit_logs",
        "TRUNCATE audit_logs",
    ):
        with pytest.raises(DBAPIError):
            await db.execute(text(statement))
        await db.rollback()
    assert await db.scalar(select(func.count()).select_from(AuditLog)) > 0

    # FK cascades (deleting a draft event) are still allowed to null the link.
    draft = (await client.post(f"{API}/events", json=_event_payload(title="Draft"),
                               headers=bearer(owner_token))).json()
    deleted = await client.delete(f"{API}/events/{draft['id']}", headers=bearer(owner_token))
    assert deleted.status_code == 204, deleted.text
    db.expire_all()
    orphaned = await db.scalar(
        select(AuditLog).where(AuditLog.entity_id == UUID(draft["id"]),
                               AuditLog.action == "event.created")
    )
    assert orphaned is not None and orphaned.event_id is None
