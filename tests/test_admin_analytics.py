"""SCRUM-17: Platform administration, suspension enforcement, monitoring,
organizer analytics, event history and the event activity timeline."""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from app.db.models import (
    Event,
    EventStaff,
    EventStaffRole,
    EventStatus,
    HoldStatus,
    InventoryHold,
    Order,
    OrderNotification,
    OrderStatus,
    Payment,
    PaymentStatus,
    TicketType,
    UserRole,
)
from tests.conftest import API, access_token_for, bearer, create_user
from tests.test_campaigns import _buyer, _campaign, _item, _order, _pay, _setup
from tests.test_orders import _make_event
from tests.test_paid_sales import _create_owner
from tests.test_refunds import _enable_refunds, _free_event, _tickets

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
needs_pg = pytest.mark.skipif(not TEST_DATABASE_URL, reason="requires real PostgreSQL")


# ---------- Helpers ----------

async def _admin(app, db, email="root@example.com"):
    user = await create_user(db, email=email, role=UserRole.PLATFORM_ADMIN,
                             first_name="Root", last_name="Admin")
    return user, access_token_for(app, user)


async def _post(client, token, path, body=None):
    return await client.post(f"{API}{path}", json=body, headers=bearer(token))


async def _get(client, token, path, **params):
    return await client.get(f"{API}{path}", headers=bearer(token), params=params)


async def _start_payment(client, token, order_id):
    start = await _post(client, token, f"/orders/{order_id}/payments/simulate")
    assert start.status_code == 201, start.text
    return start.json()["payment_id"]


async def _fresh(db, model, key):
    db.expire_all()
    return await db.get(model, UUID(str(key)))


def _d(value: str) -> str:
    return str(Decimal(value).quantize(Decimal("0.01")))


# ---------- Platform Admin permission enforcement ----------

ADMIN_GETS = [
    "/admin/users", "/admin/events", "/admin/orders", "/admin/payments",
    "/admin/activations", "/admin/refunds", "/admin/failures", "/admin/disputes",
    "/admin/support/escalated", "/admin/actions",
]


async def test_admin_endpoints_require_platform_admin(client, app, db):
    _user, user_token = await _buyer(app, db, "plain@example.com")
    _owner, owner_token = await _create_owner(client, app, db, "org-perm@example.com")
    _admin_user, admin_token = await _admin(app, db)
    target = await create_user(db, email="target@example.com")
    for path in ADMIN_GETS:
        assert (await client.get(f"{API}{path}")).status_code == 401, path
        for token in (user_token, owner_token):
            denied = await _get(client, token, path)
            assert denied.status_code == 403, path
            assert denied.json()["error"]["code"] == "INSUFFICIENT_PERMISSIONS"
        assert (await _get(client, admin_token, path)).status_code == 200, path
    # Mutations as well.
    for token in (user_token, owner_token):
        response = await _post(client, token, f"/admin/users/{target.id}/suspend",
                               {"reason": "testing"})
        assert response.status_code == 403
    assert (await _fresh(db, type(target), target.id)).status.value == "ACTIVE"


# ---------- Search and pagination ----------

async def test_admin_search_filters_and_pagination(client, app, db):
    _admin_user, admin_token = await _admin(app, db)
    for i in range(5):
        await create_user(db, email=f"needle{i}@example.com", first_name=f"Needle{i}")
    await create_user(db, email="hay@example.com")

    page = await _get(client, admin_token, "/admin/users", q="needle", limit=2, offset=0,
                      sort="email")
    body = page.json()
    assert body["total"] == 5 and len(body["items"]) == 2
    assert [u["email"] for u in body["items"]] == ["needle0@example.com", "needle1@example.com"]
    page3 = (await _get(client, admin_token, "/admin/users", q="needle", limit=2, offset=4,
                        sort="email")).json()
    assert [u["email"] for u in page3["items"]] == ["needle4@example.com"]
    assert "password" not in page.text and "hash" not in page.text
    by_id = (await _get(client, admin_token, "/admin/users",
                        q=body["items"][0]["user_id"])).json()
    assert by_id["total"] == 1

    owner, _owner_token, event, ga, _vip = await _setup(client, app, db, prefix="srch")
    _buyer_user, buyer_token = await _buyer(app, db, "srch-buyer@example.com")
    order = (await _order(client, buyer_token, event["id"], [_item(ga)])).json()
    await _pay(client, buyer_token, order["id"])
    failed = (await _order(client, buyer_token, event["id"], [_item(ga)])).json()
    await _pay(client, buyer_token, failed["id"], outcome="failed")

    organizers = (await _get(client, admin_token, "/admin/users", is_organizer=True)).json()
    assert [u["user_id"] for u in organizers["items"]] == [str(owner.id)]
    assert organizers["items"][0]["is_organizer"] is True

    events = (await _get(client, admin_token, "/admin/events", q=event["title"][:6])).json()
    assert [e["id"] for e in events["items"]] == [event["id"]]
    assert events["items"][0]["paid_sales_activated"] is True
    assert (await _get(client, admin_token, "/admin/events",
                       owner_user_id=str(uuid4()))).json()["total"] == 0

    by_number = (await _get(client, admin_token, "/admin/orders",
                            q=str(order["order_number"]))).json()
    assert [o["id"] for o in by_number["items"]] == [order["id"]]
    by_email = (await _get(client, admin_token, "/admin/orders", q="srch-buyer")).json()
    assert by_email["total"] == 2
    paid_only = (await _get(client, admin_token, "/admin/orders", status="PAID",
                            event_id=event["id"])).json()
    assert [o["id"] for o in paid_only["items"]] == [order["id"]]

    succeeded = (await _get(client, admin_token, "/admin/payments", status="SUCCEEDED")).json()
    assert succeeded["total"] == 1 and succeeded["items"][0]["order_id"] == order["id"]
    failed_pay = (await _get(client, admin_token, "/admin/payments", status="FAILED")).json()
    assert failed_pay["items"][0]["failure_code"] == "SIMULATED_FAILURE"
    by_order = (await _get(client, admin_token, "/admin/payments",
                           q=str(failed["order_number"]))).json()
    assert by_order["total"] == 1

    bad_range = await _get(client, admin_token, "/admin/orders",
                           created_from="2026-02-01T00:00:00Z", created_to="2026-01-01T00:00:00Z")
    assert bad_range.status_code == 422
    assert bad_range.json()["error"]["code"] == "INVALID_DATE_RANGE"


# ---------- User suspension ----------

async def test_user_suspension_is_enforced_for_existing_tokens(client, app, db):
    admin, admin_token = await _admin(app, db)
    other_admin, _ = await _admin(app, db, "root2@example.com")
    user = await create_user(db, email="suspect@example.com")
    login = await client.post(f"{API}/auth/login",
                              json={"email": "suspect@example.com",
                                    "password": "StrongPassword123!"})
    assert login.status_code == 200, login.text
    access, refresh = login.json()["access_token"], login.json()["refresh_token"]
    assert (await _get(client, access, "/users/me")).status_code == 200

    suspended = await _post(client, admin_token, f"/admin/users/{user.id}/suspend",
                            {"reason": "Chargeback fraud"})
    assert suspended.status_code == 200, suspended.text
    assert suspended.json()["status"] == "BLOCKED"
    assert suspended.json()["suspension_reason"] == "Chargeback fraud"

    # The already-issued access token stops working immediately.
    me = await _get(client, access, "/users/me")
    assert me.status_code == 403 and me.json()["error"]["code"] == "USER_BLOCKED"
    assert (await _get(client, access, "/orders")).status_code == 403
    assert (await client.post(f"{API}/auth/refresh",
                              json={"refresh_token": refresh})).status_code in (401, 403)
    relogin = await client.post(f"{API}/auth/login",
                                json={"email": "suspect@example.com",
                                      "password": "StrongPassword123!"})
    assert relogin.status_code == 403

    assert (await _post(client, admin_token, f"/admin/users/{user.id}/suspend",
                        {"reason": "again"})).status_code == 409
    assert (await _post(client, admin_token, f"/admin/users/{admin.id}/suspend",
                        {"reason": "self"})).json()["error"]["code"] == "CANNOT_SUSPEND_SELF"
    assert (await _post(client, admin_token, f"/admin/users/{other_admin.id}/suspend",
                        {"reason": "peer"})).json()["error"]["code"] == "CANNOT_SUSPEND_ADMIN"
    assert (await _post(client, admin_token, f"/admin/users/{user.id}/suspend",
                        {"reason": "  "})).status_code == 422

    restored = await _post(client, admin_token, f"/admin/users/{user.id}/unsuspend",
                           {"reason": "Appeal accepted"})
    assert restored.status_code == 200 and restored.json()["status"] == "ACTIVE"
    assert restored.json()["suspended_at"] is None
    assert (await _get(client, access, "/users/me")).status_code == 200

    actions = (await _get(client, admin_token, "/admin/actions",
                          entity_id=str(user.id))).json()["items"]
    assert [a["action"] for a in actions] == ["admin.user_unsuspended", "admin.user_suspended"]
    suspend_entry = actions[1]
    assert suspend_entry["admin_user_id"] == str(admin.id)
    assert suspend_entry["reason"] == "Chargeback fraud"
    assert suspend_entry["before"] == {"status": "ACTIVE"}
    assert suspend_entry["after"] == {"status": "BLOCKED"}
    assert suspend_entry["entity_type"] == "user"


# ---------- Event suspension ----------

async def test_event_suspension_blocks_every_sales_entry_point(client, app, db):
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="evs")
    _admin_user, admin_token = await _admin(app, db)
    await _campaign(client, owner_token, event["id"])
    _paid_buyer, paid_token = await _buyer(app, db, "evs-paid@example.com")
    paid_order = (await _order(client, paid_token, event["id"], [_item(ga)])).json()
    await _pay(client, paid_token, paid_order["id"])
    _p1, p1_token = await _buyer(app, db, "evs-p1@example.com")
    pending_with_payment = (await _order(client, p1_token, event["id"], [_item(ga)])).json()
    payment_id = await _start_payment(client, p1_token, pending_with_payment["id"])
    _p2, p2_token = await _buyer(app, db, "evs-p2@example.com")
    pending = (await _order(client, p2_token, event["id"], [_item(ga)])).json()

    response = await _post(client, admin_token, f"/admin/events/{event['id']}/suspend",
                           {"reason": "Suspicious listing"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["event"]["is_suspended"] is True
    assert body["event"]["status"] == "PUBLISHED"  # lifecycle status unchanged
    assert body["pending_orders"] == 2 and body["confirmed_orders"] == 1

    _late, late_token = await _buyer(app, db, "evs-late@example.com")
    blocked = await _order(client, late_token, event["id"], [_item(ga)])
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "EVENT_SUSPENDED"
    legacy = await _post(client, late_token,
                         f"/events/{event['id']}/ticket-types/{ga['id']}/purchase",
                         {"quantity": 1})
    assert legacy.status_code == 409 and legacy.json()["error"]["code"] == "EVENT_SUSPENDED"
    hold = await _post(client, late_token, f"/events/{event['id']}/ticket-types/{ga['id']}/holds",
                       {"quantity": 1})
    assert hold.status_code == 409
    quote = await _post(client, late_token, "/campaigns/validate",
                        {"event_id": event["id"], "code": "spring10", "items": [_item(ga)]})
    assert quote.status_code == 409

    # Pending checkouts are not silently cancelled, but cannot complete.
    assert (await _fresh(db, Order, pending["id"])).status == OrderStatus.PENDING
    start = await _post(client, p2_token, f"/orders/{pending['id']}/payments/simulate")
    assert start.status_code == 409 and start.json()["error"]["code"] == "EVENT_SUSPENDED"
    resolved = await _post(client, p1_token, f"/orders/payments/{payment_id}/simulate-result",
                           {"outcome": "success"})
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "FAILED"
    assert resolved.json()["failure_code"] == "EVENT_SUSPENDED"
    assert (await _fresh(db, Order, pending_with_payment["id"])).status == OrderStatus.CANCELLED
    assert await _tickets(db, pending_with_payment["id"]) == []
    holds = (await db.execute(select(InventoryHold).where(
        InventoryHold.order_id == UUID(pending_with_payment["id"])))).scalars().all()
    assert {h.status for h in holds} == {HoldStatus.RELEASED}

    # Valid tickets and historical financial records are untouched.
    stored = await _fresh(db, Order, paid_order["id"])
    assert stored.status == OrderStatus.PAID and stored.total_amount == Decimal("25.75")
    assert {t.status.value for t in await _tickets(db, paid_order["id"])} == {"VALID"}
    paid_payment = await db.scalar(select(Payment).where(
        Payment.order_id == UUID(paid_order["id"])))
    assert paid_payment.status == PaymentStatus.SUCCEEDED

    # No alternate route back to sale; hidden from the public.
    assert (await _post(client, owner_token,
                        f"/events/{event['id']}/publish")).json()["error"]["code"] == \
        "EVENT_SUSPENDED"
    assert (await _post(client, owner_token,
                        f"/events/{event['id']}/duplicate")).status_code == 409
    catalog = await client.get(f"{API}/events/catalog")
    assert event["id"] not in [e["id"] for e in catalog.json()]
    assert (await client.get(f"{API}/events/{event['id']}")).status_code == 404
    own_view = await _get(client, owner_token, f"/events/{event['id']}")
    assert own_view.status_code == 200 and own_view.json()["is_suspended"] is True

    # Refunds of existing orders still follow their lifecycle.
    await _enable_refunds(client, owner_token, event["id"])
    refund = await _post(client, paid_token, f"/orders/{paid_order['id']}/refund")
    assert refund.status_code == 200 and refund.json()["order_status"] == "REFUNDED"

    assert (await _post(client, admin_token, f"/admin/events/{event['id']}/suspend",
                        {"reason": "twice"})).status_code == 409
    lifted = await _post(client, admin_token, f"/admin/events/{event['id']}/unsuspend",
                         {"reason": "Verified organizer"})
    assert lifted.status_code == 200 and lifted.json()["event"]["is_suspended"] is False
    again = await _order(client, late_token, event["id"], [_item(ga)])
    assert again.status_code == 201, again.text
    paid_again = await _pay(client, late_token, again.json()["id"])
    assert paid_again["status"] == "SUCCEEDED"

    actions = (await _get(client, admin_token, "/admin/actions",
                          entity_id=event["id"])).json()["items"]
    assert [a["action"] for a in actions] == ["admin.event_unsuspended", "admin.event_suspended"]
    assert actions[1]["reason"] == "Suspicious listing"
    assert actions[1]["before"]["suspended"] is False and actions[1]["after"]["suspended"]


async def test_stop_paid_sales_keeps_free_registration(client, app, db):
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="stop")
    _admin_user, admin_token = await _admin(app, db)
    free = await _post(client, owner_token, f"/events/{event['id']}/ticket-types",
                       {"name": "Community", "price": "0.00", "currency": "KZT",
                        "capacity": 5, "min_per_order": 1, "max_per_order": 2})
    assert free.status_code == 201, free.text
    _early, early_token = await _buyer(app, db, "stop-early@example.com")
    early_order = (await _order(client, early_token, event["id"], [_item(ga)])).json()
    payment_id = await _start_payment(client, early_token, early_order["id"])

    stopped = await _post(client, admin_token, f"/admin/events/{event['id']}/paid-sales/stop",
                          {"reason": "Payout account under review"})
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["event"]["paid_sales_suspended"] is True
    assert (await _post(client, admin_token, f"/admin/events/{event['id']}/paid-sales/stop",
                        {"reason": "again"})).status_code == 409

    _buyer_user, buyer_token = await _buyer(app, db, "stop-buyer@example.com")
    paid = await _order(client, buyer_token, event["id"], [_item(ga)])
    assert paid.status_code == 409 and paid.json()["error"]["code"] == "PAID_SALES_SUSPENDED"
    registration = await _order(client, buyer_token, event["id"], [_item(free.json())])
    assert registration.status_code == 201, registration.text
    assert registration.json()["status"] == "PAID" and registration.json()["total_amount"] == "0.00"

    finished = await _post(client, early_token, f"/orders/payments/{payment_id}/simulate-result",
                           {"outcome": "success"})
    assert finished.json()["status"] == "FAILED"
    assert finished.json()["failure_code"] == "PAID_SALES_SUSPENDED"

    status = (await _get(client, owner_token,
                         f"/events/{event['id']}/paid-sales/activation")).json()
    assert status["is_activated"] is True and status["sales_suspended"] is True
    assert status["paid_sales_open"] is False
    reactivate = await _post(client, owner_token, f"/events/{event['id']}/paid-sales/activate")
    assert reactivate.status_code == 409

    monitored = (await _get(client, admin_token, "/admin/activations",
                            status="SUSPENDED")).json()
    assert [a["event_id"] for a in monitored["items"]] == [event["id"]]
    assert monitored["items"][0]["sales_suspension_reason"] == "Payout account under review"

    resumed = await _post(client, admin_token, f"/admin/events/{event['id']}/paid-sales/resume",
                          {"reason": "Review complete"})
    assert resumed.status_code == 200
    assert (await _order(client, buyer_token, event["id"], [_item(ga)])).status_code == 201
    assert (await _get(client, admin_token, "/admin/activations",
                       status="ACTIVE")).json()["total"] == 1


# ---------- Monitoring ----------

async def test_admin_monitoring_refunds_failures_disputes(client, app, db):
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="mon")
    _admin_user, admin_token = await _admin(app, db)
    await _enable_refunds(client, owner_token, event["id"])
    _buyer_user, token = await _buyer(app, db, "mon-buyer@example.com")
    kept = (await _order(client, token, event["id"], [_item(ga)])).json()
    await _pay(client, token, kept["id"])
    refunded = (await _order(client, token, event["id"], [_item(ga)])).json()
    await _pay(client, token, refunded["id"])
    await _post(client, token, f"/orders/{refunded['id']}/refund")
    declined = (await _order(client, token, event["id"], [_item(ga)])).json()
    await _pay(client, token, declined["id"], outcome="failed")

    refunds = (await _get(client, admin_token, "/admin/refunds", status="SUCCEEDED")).json()
    assert refunds["total"] == 1
    assert refunds["items"][0]["order_id"] == refunded["id"]
    assert refunds["items"][0]["amount"] == "25.75"
    assert refunds["items"][0]["kind"] == "CUSTOMER_REQUEST"

    # A failed outbox email (authoritative row, not synthesized).
    notification = await db.scalar(select(OrderNotification).where(
        OrderNotification.order_id == UUID(kept["id"])))
    notification.status = "FAILED"
    notification.attempts = 3
    notification.last_error = "permanent: invalid recipient"
    await db.commit()

    failures = (await _get(client, admin_token, "/admin/failures")).json()
    kinds = {(f["kind"], f["code"]) for f in failures["items"]}
    assert ("PAYMENT", "SIMULATED_FAILURE") in kinds
    assert ("NOTIFICATION", "ORDER_CONFIRMATION") in kinds
    notif = next(f for f in failures["items"] if f["kind"] == "NOTIFICATION")
    assert notif["attempts"] == 3 and notif["event_id"] == event["id"]
    only_payments = (await _get(client, admin_token, "/admin/failures", kind="PAYMENT")).json()
    assert {f["kind"] for f in only_payments["items"]} == {"PAYMENT"}
    assert (await _get(client, admin_token, "/admin/failures", kind="REFUND")).json()["total"] == 0

    kept_payment = await db.scalar(select(Payment).where(Payment.order_id == UUID(kept["id"])))
    failed_payment = await db.scalar(select(Payment).where(
        Payment.order_id == UUID(declined["id"])))
    kept_payment_id, failed_payment_id = kept_payment.id, failed_payment.id
    opened = await _post(client, admin_token, "/admin/disputes",
                         {"payment_id": str(kept_payment_id), "reason": "Customer chargeback"})
    assert opened.status_code == 201, opened.text
    dispute = opened.json()
    assert dispute["status"] == "OPEN" and dispute["amount"] == "25.75"
    assert dispute["event_id"] == event["id"]
    duplicate = await _post(client, admin_token, "/admin/disputes",
                            {"payment_id": str(kept_payment_id), "reason": "again"})
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "DISPUTE_ALREADY_OPEN"
    not_captured = await _post(client, admin_token, "/admin/disputes",
                               {"payment_id": str(failed_payment_id), "reason": "nope"})
    assert not_captured.status_code == 409
    too_much = await _post(client, admin_token, "/admin/disputes",
                           {"payment_id": str(kept_payment_id), "reason": "x" * 5,
                            "amount": "999.00"})
    assert too_much.status_code in (409, 422)

    base = f"{API}/admin/disputes/{dispute['id']}"
    review = await client.patch(base, json={"status": "UNDER_REVIEW"},
                                headers=bearer(admin_token))
    assert review.json()["status"] == "UNDER_REVIEW"
    done = await client.patch(base, json={"status": "RESOLVED",
                                          "resolution_note": "Evidence accepted"},
                              headers=bearer(admin_token))
    assert done.json()["status"] == "RESOLVED" and done.json()["resolved_at"] is not None
    reopen = await client.patch(base, json={"status": "OPEN"}, headers=bearer(admin_token))
    assert reopen.status_code == 409
    listed = (await _get(client, admin_token, "/admin/disputes", status="RESOLVED")).json()
    assert [d["id"] for d in listed["items"]] == [dispute["id"]]
    # Bookkeeping only: the payment is untouched.
    assert (await _fresh(db, Payment, kept_payment_id)).status == PaymentStatus.SUCCEEDED
    dispute_actions = (await _get(client, admin_token, "/admin/actions",
                                  entity_type="payment_dispute")).json()
    assert dispute_actions["total"] == 3


# ---------- Analytics authorization ----------

async def test_organizers_see_analytics_only_for_events_they_manage(client, app, db):
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="auz")
    _other, other_token, other_event, other_ga, _ = await _setup(client, app, db, prefix="auz2")
    _buyer_user, buyer_token = await _buyer(app, db, "auz-buyer@example.com")
    staff = await create_user(db, email="auz-staff@example.com")
    db.add(EventStaff(event_id=UUID(event["id"]), user_id=staff.id,
                      role=EventStaffRole.EVENT_ADMIN,
                      permissions={"view_event": True, "check_in_tickets": True}))
    analyst = await create_user(db, email="auz-analyst@example.com")
    db.add(EventStaff(event_id=UUID(event["id"]), user_id=analyst.id,
                      role=EventStaffRole.ORGANIZER_STAFF,
                      permissions={"view_analytics": True}))
    await db.commit()
    staff_token = access_token_for(app, staff)
    analyst_token = access_token_for(app, analyst)
    _admin_user, admin_token = await _admin(app, db)

    path = f"/events/{event['id']}/analytics"
    assert (await _get(client, owner_token, path)).status_code == 200
    assert (await _get(client, analyst_token, path)).status_code == 200
    assert (await _get(client, admin_token, path)).status_code == 200
    for token in (other_token, buyer_token, staff_token):
        denied = await _get(client, token, path)
        assert denied.status_code == 403, denied.text
    # Indirect references through the event route.
    assert (await _get(client, owner_token, path,
                       ticket_type_id=other_ga["id"])).status_code == 404
    assert (await _get(client, owner_token, f"/events/{uuid4()}/analytics")).status_code == 404

    agg = "/organizers/me/analytics"
    mine = (await _get(client, other_token, agg)).json()
    assert mine["scope"]["event_ids"] == [other_event["id"]]
    assert (await _get(client, other_token, agg, event_id=event["id"])).status_code == 403
    assert (await _get(client, other_token, agg, ticket_type_id=ga["id"])).status_code == 404
    assert (await _get(client, other_token, agg, event_id=str(uuid4()))).status_code == 404
    narrowed = await _get(client, owner_token, agg, event_id=event["id"],
                          ticket_type_id=other_ga["id"])
    assert narrowed.status_code == 404
    # Date filters never widen the scope.
    ranged = (await _get(client, other_token, agg, start="2020-01-01T00:00:00Z",
                         end="2030-01-01T00:00:00Z")).json()
    assert ranged["scope"]["event_ids"] == [other_event["id"]]
    # Relationship based, not role based.
    assert (await _get(client, buyer_token, agg)).json()["scope"]["event_count"] == 0
    assert (await _get(client, admin_token, agg)).json()["scope"]["event_count"] == 0
    assert (await _get(client, staff_token, agg)).json()["scope"]["event_count"] == 0
    assert (await _get(client, analyst_token, agg)).json()["scope"]["event_ids"] == [event["id"]]
    bad = await _get(client, owner_token, path, start="2026-05-01T00:00:00Z",
                     end="2026-04-01T00:00:00Z")
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "INVALID_DATE_RANGE"


# ---------- Analytics accuracy ----------

async def test_analytics_figures_match_order_payment_and_refund_records(client, app, db):
    _owner, owner_token, event, ga, vip = await _setup(client, app, db, prefix="acc")
    await _campaign(client, owner_token, event["id"], ticket_type_ids=[ga["id"]])
    await _enable_refunds(client, owner_token, event["id"])

    # A: 2 x GA with 10% promo -> 50 - 5 + 1.35 fee = 46.35
    _a, a_token = await _buyer(app, db, "acc-a@example.com")
    a = (await _order(client, a_token, event["id"], [_item(ga, 2)], promo_code="spring10")).json()
    await _pay(client, a_token, a["id"])
    # B: 1 x VIP -> 100 + 3 fee = 103.00
    _b, b_token = await _buyer(app, db, "acc-b@example.com")
    b = (await _order(client, b_token, event["id"], [_item(vip)])).json()
    await _pay(client, b_token, b["id"])
    # C: pending (unpaid) -> excluded from sales, holds 1 GA
    _c, c_token = await _buyer(app, db, "acc-c@example.com")
    await _order(client, c_token, event["id"], [_item(ga)])
    # D: failed payment -> excluded
    _dd, d_token = await _buyer(app, db, "acc-d@example.com")
    d = (await _order(client, d_token, event["id"], [_item(ga)])).json()
    await _pay(client, d_token, d["id"], outcome="failed")
    # E: paid then refunded -> 25.75 sold and 25.75 refunded
    _e, e_token = await _buyer(app, db, "acc-e@example.com")
    e = (await _order(client, e_token, event["id"], [_item(ga)])).json()
    await _pay(client, e_token, e["id"])
    assert (await _post(client, e_token, f"/orders/{e['id']}/refund")).status_code == 200

    data = (await _get(client, owner_token, f"/events/{event['id']}/analytics")).json()
    assert data["definitions"]["sales"]
    [kzt] = data["sales"]
    assert kzt == {
        "currency": "KZT", "orders": 3, "tickets_sold": 4, "tickets_reversed": 1,
        "gross_ticket_sales": _d("175"), "discounts": _d("5"), "ticket_revenue": _d("170"),
        "fees": _d("5.10"), "gross_revenue": _d("175.10"), "refund_count": 1,
        "refunds": _d("25.75"), "net_revenue": _d("149.35"),
    }
    # gross_revenue equals what customers were charged (no double discount).
    charged = sum(
        p.amount for p in (await db.execute(select(Payment).where(
            Payment.status.in_((PaymentStatus.SUCCEEDED, PaymentStatus.REFUNDED))
        ))).scalars().all()
    )
    assert Decimal(kzt["gross_revenue"]) == charged

    assert data["capacity"] == {"total_capacity": 20, "sold_tickets": 3,
                                "reserved_tickets": 1, "remaining_tickets": 16}
    types = {t["name"]: t for t in data["ticket_types"]}
    assert types["General Admission"]["sold_tickets"] == 2
    assert types["General Admission"]["reserved_tickets"] == 1
    assert types["General Admission"]["remaining_tickets"] == 7
    assert types["General Admission"]["period_tickets_sold"] == 3
    assert types["General Admission"]["period_tickets_reversed"] == 1
    assert types["General Admission"]["period_revenue"] == _d("70")
    assert types["General Admission"]["period_discounts"] == _d("5")
    assert types["VIP"]["sold_tickets"] == 1 and types["VIP"]["period_revenue"] == _d("100")
    assert data["attendance"]["eligible_tickets"] == 3
    assert data["attendance"]["refunded_tickets"] == 1
    assert data["attendance"]["attendance_rate"] == 0.0

    [campaign] = data["campaigns"]
    assert campaign["successful_redemptions"] == 1 and campaign["reserved_redemptions"] == 0
    assert campaign["sales"] == [{"currency": "KZT", "orders": 1, "tickets_sold": 2,
                                  "reversed_orders": 0, "discounts": _d("5"),
                                  "revenue": _d("46.35")}]

    # Ticket type filter: item level, fees not attributable.
    ga_only = (await _get(client, owner_token, f"/events/{event['id']}/analytics",
                          ticket_type_id=ga["id"])).json()
    [ga_sales] = ga_only["sales"]
    assert ga_sales["orders"] == 2 and ga_sales["tickets_sold"] == 3
    assert ga_sales["fees"] is None
    assert ga_sales["gross_revenue"] == _d("70") and ga_sales["refunds"] == _d("25")
    assert ga_sales["net_revenue"] == _d("45")
    assert [t["name"] for t in ga_only["ticket_types"]] == ["General Admission"]
    assert ga_only["capacity"]["total_capacity"] == 10
    assert ga_only["campaigns"][0]["sales"][0]["revenue"] == _d("45")

    # Date range filters historical metrics only; snapshots are unchanged.
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    later = (await _get(client, owner_token, f"/events/{event['id']}/analytics",
                        start=future)).json()
    assert later["sales"] == []
    assert later["capacity"] == data["capacity"]
    assert later["campaigns"][0]["sales"] == []
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    until_yesterday = (await _get(client, owner_token, f"/events/{event['id']}/analytics",
                                  end=past)).json()
    assert until_yesterday["sales"] == []

    # Aggregated view across the organizer's events equals the single event.
    agg = (await _get(client, owner_token, "/organizers/me/analytics")).json()
    assert agg["sales"] == data["sales"]


async def test_attendance_ignores_repeated_and_reversed_check_ins(client, app, db):
    owner_token, event, free_type = await _free_event(client, app, db, "att", capacity=10)
    _b1, t1 = await _buyer(app, db, "att-1@example.com")
    order = (await _order(client, t1, event["id"], [_item(free_type, 3)])).json()
    _b2, t2 = await _buyer(app, db, "att-2@example.com")
    cancelled = (await _order(client, t2, event["id"], [_item(free_type)])).json()
    assert (await _post(client, t2, f"/orders/{cancelled['id']}/cancel")).status_code == 200

    tickets = (await _get(client, t1, f"/orders/{order['id']}/tickets")).json()
    first, second = tickets[0]["public_id"], tickets[1]["public_id"]
    base = f"/events/{event['id']}/tickets"
    assert (await _post(client, owner_token, f"{base}/{first}/check-in")).status_code == 200
    repeat = await _post(client, owner_token, f"{base}/{first}/check-in")
    assert repeat.status_code == 409  # a repeated scan creates nothing
    assert (await _post(client, owner_token, f"{base}/{second}/check-in")).status_code == 200
    assert (await _post(client, owner_token,
                        f"{base}/{second}/reverse-check-in")).status_code == 200

    att = (await _get(client, owner_token, f"/events/{event['id']}/analytics")).json()
    assert att["attendance"] == {
        "checked_in": 1, "eligible_tickets": 3, "attendance_rate": 0.3333,
        "cancelled_tickets": 1, "refunded_tickets": 0,
        "period_check_ins": 2, "period_reversals": 1, "period_admitted_tickets": 1,
    }
    [sales] = att["sales"]  # free registrations: counted, zero revenue
    assert sales["tickets_sold"] == 4 and sales["tickets_reversed"] == 1
    assert sales["gross_revenue"] == _d("0")

    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    later = (await _get(client, owner_token, f"/events/{event['id']}/analytics",
                        start=future)).json()["attendance"]
    assert later["checked_in"] == 1 and later["attendance_rate"] == 0.3333  # snapshot
    assert later["period_check_ins"] == later["period_admitted_tickets"] == 0


# ---------- Event history ----------

async def test_event_history_categories_filters_and_scope(client, app, db):
    owner, owner_token = await _create_owner(client, app, db, "hist-owner@example.com")
    _admin_user, admin_token = await _admin(app, db)
    now = datetime.now(timezone.utc)
    created = {}
    for title in ("Upcoming gala", "Live show", "Past show", "Cancelled show",
                  "Suspended gala"):
        event, _tt = await _make_event(client, owner_token, title=title)
        created[title] = event["id"]
    draft = await _post(client, owner_token, "/events", {
        "title": "Draft idea", "timezone": "UTC",
        "starts_at": (now + timedelta(days=9)).isoformat(),
        "ends_at": (now + timedelta(days=9, hours=2)).isoformat(),
    })
    assert draft.status_code == 201, draft.text
    created["Draft idea"] = draft.json()["id"]

    await db.execute(update(Event).where(Event.id == UUID(created["Live show"])).values(
        starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=2)))
    await db.execute(update(Event).where(Event.id == UUID(created["Past show"])).values(
        starts_at=now - timedelta(days=3), ends_at=now - timedelta(days=2)))
    await db.commit()
    assert (await _post(client, owner_token,
                        f"/events/{created['Cancelled show']}/cancel")).status_code == 200
    assert (await _post(client, admin_token, f"/admin/events/{created['Suspended gala']}/suspend",
                        {"reason": "review"})).status_code == 200

    # Staff assignment on another organizer's event; and an unrelated event.
    _other, other_token = await _create_owner(client, app, db, "hist-other@example.com")
    staffed, _ = await _make_event(client, other_token, title="Partner fest")
    hidden, _ = await _make_event(client, other_token, title="Not yours")
    db.add(EventStaff(event_id=UUID(staffed["id"]), user_id=owner.id,
                      role=EventStaffRole.EVENT_ADMIN, permissions={}))
    await db.commit()

    page = (await _get(client, owner_token, "/organizers/me/events", limit=100)).json()
    by_title = {e["title"]: e for e in page["items"]}
    assert page["total"] == 7
    assert "Not yours" not in by_title
    assert {t: e["category"] for t, e in by_title.items()} == {
        "Upcoming gala": "UPCOMING", "Live show": "ACTIVE", "Past show": "COMPLETED",
        "Cancelled show": "CANCELLED", "Suspended gala": "UPCOMING",
        "Draft idea": "UPCOMING", "Partner fest": "UPCOMING",
    }
    assert by_title["Suspended gala"]["is_suspended"] is True
    assert by_title["Suspended gala"]["status"] == "PUBLISHED"
    assert by_title["Partner fest"]["relationship"] == "EVENT_ADMIN"
    assert by_title["Upcoming gala"]["relationship"] == "OWNER"

    def titles(**params):
        return client.get(f"{API}/organizers/me/events", params=params,
                          headers=bearer(owner_token))

    active = (await titles(category="ACTIVE")).json()
    assert [e["title"] for e in active["items"]] == ["Live show"]
    done = (await titles(category=["COMPLETED", "CANCELLED"], sort="title")).json()
    assert [e["title"] for e in done["items"]] == ["Cancelled show", "Past show"]
    assert [e["title"] for e in (await titles(q="gala", sort="title")).json()["items"]] == [
        "Suspended gala", "Upcoming gala"]
    assert (await titles(suspended=True)).json()["total"] == 1
    assert (await titles(status="DRAFT")).json()["items"][0]["title"] == "Draft idea"
    window = (await titles(starts_from=(now - timedelta(days=4)).isoformat(),
                           starts_to=(now - timedelta(days=1)).isoformat())).json()
    assert [e["title"] for e in window["items"]] == ["Past show"]
    paged = (await titles(limit=2, offset=0, sort="title")).json()
    assert paged["total"] == 7 and [e["title"] for e in paged["items"]] == [
        "Cancelled show", "Draft idea"]
    assert (await titles(category="ARCHIVED")).status_code == 422

    outsider_view = (await _get(client, other_token, "/organizers/me/events")).json()
    assert {e["title"] for e in outsider_view["items"]} == {"Partner fest", "Not yours"}
    del hidden


# ---------- Activity timeline ----------

async def test_event_timeline_ordering_filters_and_redaction(client, app, db):
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="tl")
    _other, other_token = await _create_owner(client, app, db, "tl-other@example.com")
    admin, admin_token = await _admin(app, db)
    await _campaign(client, owner_token, event["id"])
    _buyer_user, buyer_token = await _buyer(app, db, "tl-buyer@example.com")
    order = (await _order(client, buyer_token, event["id"], [_item(ga)])).json()
    await _pay(client, buyer_token, order["id"])
    ticket = (await _get(client, buyer_token, f"/orders/{order['id']}/tickets")).json()[0]
    await _post(client, owner_token,
                f"/events/{event['id']}/tickets/{ticket['public_id']}/check-in")
    await _post(client, owner_token,
                f"/events/{event['id']}/tickets/{ticket['public_id']}/reverse-check-in")
    await _post(client, buyer_token, "/support/cases",
                {"subject": "Parking?", "category": "EVENT_INFORMATION",
                 "message": "Is there parking?", "event_id": event["id"]})
    await _post(client, admin_token, f"/admin/events/{event['id']}/paid-sales/stop",
                {"reason": "Check"})
    # A denied probe is audited but is not event activity.
    assert (await _get(client, other_token, f"/events/{event['id']}/inventory")).status_code == 403

    path = f"/events/{event['id']}/timeline"
    timeline = (await _get(client, owner_token, path, limit=200)).json()
    actions = [entry["action"] for entry in timeline["items"]]
    for expected in ("event.created", "ticket_type.created", "event.published",
                     "campaign.created", "order.created", "order.paid",
                     "payment.state_changed", "ticket.checked_in", "ticket.check_in_reversed",
                     "support_case.created", "admin.paid_sales_stopped"):
        assert expected in actions, expected
    assert "event.staff_access_denied" not in actions
    assert timeline["total"] == len(timeline["items"])
    stamps = [entry["timestamp"] for entry in timeline["items"]]
    assert stamps == sorted(stamps, reverse=True)
    for entry in timeline["items"]:
        assert set(entry) == {"id", "timestamp", "action", "entity_type", "entity_id",
                              "actor", "description"}
        assert entry["description"]
    admin_entry = next(e for e in timeline["items"] if e["action"] == "admin.paid_sales_stopped")
    assert admin_entry["actor"] == {"user_id": str(admin.id), "type": "PLATFORM_ADMIN",
                                    "display_name": "Root Admin"}
    payment_entry = next(e for e in timeline["items"] if e["action"] == "payment.state_changed")
    assert payment_entry["description"] == "Payment changed from PENDING to SUCCEEDED"
    assert payment_entry["entity_type"] == "payment"

    asc = (await _get(client, owner_token, path, order="asc", limit=200)).json()
    assert [e["id"] for e in asc["items"]] == [e["id"] for e in reversed(timeline["items"])]
    assert asc["items"][0]["action"] == "event.created"

    orders_only = (await _get(client, owner_token, path, action="order.")).json()
    assert {e["action"] for e in orders_only["items"]} == {"order.created", "order.paid"}
    exact = (await client.get(f"{API}{path}",
                              params=[("action", "event.published"), ("action", "ticket.*")],
                              headers=bearer(owner_token))).json()
    assert {e["action"] for e in exact["items"]} == {
        "event.published", "ticket.checked_in", "ticket.check_in_reversed"}
    support = (await _get(client, owner_token, path, entity_type="support_case")).json()
    assert [e["action"] for e in support["items"]] == ["support_case.created"]
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    assert (await _get(client, owner_token, path, start=future)).json()["total"] == 0
    page = (await _get(client, owner_token, path, limit=3, offset=3)).json()
    assert [e["id"] for e in page["items"]] == [e["id"] for e in timeline["items"][3:6]]

    assert (await _get(client, other_token, path)).status_code == 403
    assert (await _get(client, buyer_token, path)).status_code == 403
    assert (await _get(client, admin_token, path)).status_code == 200


# ---------- Read-only analytics vs. checkout ----------

@needs_pg
async def test_analytics_does_not_wait_on_checkout_row_locks(client, app, db):
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="lock")
    _buyer_user, token = await _buyer(app, db, "lock-buyer@example.com")
    order = (await _order(client, token, event["id"], [_item(ga)])).json()
    await _pay(client, token, order["id"])

    # Hold the same row locks an in-flight checkout holds (event, ticket type,
    # order) in a separate transaction, then run analytics and the timeline.
    async with app.state.engine.connect() as conn:
        tx = await conn.begin()
        await conn.execute(select(Event).where(Event.id == UUID(event["id"])).with_for_update())
        await conn.execute(select(TicketType).where(
            TicketType.event_id == UUID(event["id"])).with_for_update())
        await conn.execute(select(Order).where(
            Order.id == UUID(order["id"])).with_for_update())
        analytics = await asyncio.wait_for(
            _get(client, owner_token, f"/events/{event['id']}/analytics"), timeout=10
        )
        assert analytics.status_code == 200
        assert analytics.json()["sales"][0]["tickets_sold"] == 1
        aggregated = await asyncio.wait_for(
            _get(client, owner_token, "/organizers/me/analytics"), timeout=10
        )
        assert aggregated.status_code == 200
        await tx.rollback()

    # And checkout still works normally afterwards (regression).
    second = await _order(client, token, event["id"], [_item(ga)])
    assert second.status_code == 201
    assert (await _pay(client, token, second.json()["id"]))["status"] == "SUCCEEDED"
    assert {t.status.value for t in await _tickets(db, second.json()["id"])} == {"VALID"}


async def test_checkout_and_ticket_issuance_regression(client, app, db):
    """Free and paid checkout still issue tickets with the new guards in place."""
    _owner_token, event, free_type = await _free_event(client, app, db, "reg", capacity=3)
    _b, token = await _buyer(app, db, "reg-buyer@example.com")
    free_order = (await _order(client, token, event["id"], [_item(free_type, 2)])).json()
    assert free_order["status"] == "PAID" and len(free_order["tickets"]) == 2

    _o, _paid_owner_token, paid_event, ga, _vip = await _setup(client, app, db, prefix="reg2")
    paid = (await _order(client, token, paid_event["id"], [_item(ga)])).json()
    assert paid["status"] == "PENDING"
    result = await _pay(client, token, paid["id"])
    assert result["status"] == "SUCCEEDED"
    stored = await _fresh(db, Order, paid["id"])
    assert stored.status == OrderStatus.PAID
    assert {t.status.value for t in await _tickets(db, paid["id"])} == {"VALID"}
    assert (await _fresh(db, Event, paid_event["id"])).status == EventStatus.PUBLISHED
