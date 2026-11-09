"""Integration tests for event activation and simulated paid checkout."""

from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.db.models import (
    EventSalesActivation,
    HoldStatus,
    InventoryHold,
    Order,
    OrderItem,
    OrderStatus,
    Payment,
    PaymentStatus,
    Ticket,
)
from tests.conftest import API, access_token_for, bearer, create_user
from tests.test_orders import _activate_paid_sales, _make_event


async def _create_owner(client, app, db, email):
    owner = await create_user(db, email=email)
    token = access_token_for(app, owner)
    profile = await client.post(
        f"{API}/organizers/me",
        json={"display_name": "Paid Sales Organizer", "terms_accepted": True},
        headers=bearer(token),
    )
    assert profile.status_code == 201, profile.text
    return owner, token


async def _make_paid_event(client, app, db, *, email="paid-sales-owner@example.com", capacity=5):
    owner, owner_token = await _create_owner(client, app, db, email)
    event, ticket_type = await _make_event(client, owner_token, capacity=capacity)
    await _activate_paid_sales(client, db, owner, owner_token, event["id"])
    return owner, owner_token, event, ticket_type


async def _create_paid_order(
    client, app, db, event, ticket_type, *, email="paid-buyer@example.com"
):
    buyer = await create_user(db, email=email)
    token = access_token_for(app, buyer)
    response = await client.post(
        f"{API}/orders",
        json={
            "event_id": event["id"],
            "items": [{"ticket_type_id": ticket_type["id"], "quantity": 1}],
        },
        headers=bearer(token),
    )
    return buyer, token, response


async def _count_for_order(db, model, order_id):
    if model is Ticket:
        statement = (
            select(func.count(Ticket.id))
            .join(OrderItem, Ticket.order_item_id == OrderItem.id)
            .where(OrderItem.order_id == order_id)
        )
    else:
        statement = select(func.count()).select_from(model).where(model.order_id == order_id)
    return await db.scalar(statement)


async def test_paid_ticket_purchase_is_blocked_before_event_activation(client, app, db):
    _owner, owner_token = await _create_owner(client, app, db, "inactive-owner@example.com")
    event, ticket_type = await _make_event(client, owner_token)
    _buyer, token, response = await _create_paid_order(
        client, app, db, event, ticket_type, email="blocked-buyer@example.com"
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PAID_SALES_NOT_ACTIVATED"
    assert await db.scalar(select(func.count()).select_from(Order)) == 0
    assert await db.scalar(select(func.count()).select_from(Ticket)) == 0


async def test_activation_checklist_is_event_scoped(client, app, db):
    owner, owner_token = await _create_owner(client, app, db, "scope-owner@example.com")
    event_a, ticket_a = await _make_event(client, owner_token, capacity=4)
    event_b, ticket_b = await _make_event(client, owner_token, capacity=4, title="Second event")

    before = await client.post(
        f"{API}/events/{event_a['id']}/paid-sales/activate", headers=bearer(owner_token)
    )
    assert before.status_code == 409
    assert before.json()["error"]["code"] == "ACTIVATION_CHECKLIST_INCOMPLETE"

    await _activate_paid_sales(client, db, owner, owner_token, event_a["id"])
    activation_a = await db.get(EventSalesActivation, UUID(event_a["id"]))
    activation_b = await db.get(EventSalesActivation, UUID(event_b["id"]))
    assert activation_a is not None and activation_a.activated_at is not None
    assert activation_b is None

    _, _, ok_response = await _create_paid_order(
        client, app, db, event_a, ticket_a, email="scope-buyer-a@example.com"
    )
    _, _, blocked_response = await _create_paid_order(
        client, app, db, event_b, ticket_b, email="scope-buyer-b@example.com"
    )
    assert ok_response.status_code == 201, ok_response.text
    assert blocked_response.status_code == 409
    assert blocked_response.json()["error"]["code"] == "PAID_SALES_NOT_ACTIVATED"


async def test_successful_payment_issues_tickets_once_and_is_idempotent(client, app, db):
    _owner, _owner_token, event, ticket_type = await _make_paid_event(
        client, app, db, email="success-owner@example.com"
    )
    buyer, buyer_token, order_response = await _create_paid_order(
        client, app, db, event, ticket_type, email="success-buyer@example.com"
    )
    assert order_response.status_code == 201, order_response.text
    order_body = order_response.json()
    order_id = UUID(order_body["id"])

    # 25.00 KZT ticket + 3% processing fee, calculated by the backend.
    assert order_body["subtotal_amount"] == "25.00"
    assert order_body["fee_amount"] == "0.75"
    assert order_body["total_amount"] == "25.75"
    assert order_body["status"] == "PENDING"
    assert await _count_for_order(db, Ticket, order_id) == 0

    payment_start = await client.post(
        f"{API}/orders/{order_id}/payments/simulate", headers=bearer(buyer_token)
    )
    assert payment_start.status_code == 201, payment_start.text
    payment_body = payment_start.json()
    assert payment_body["is_simulated"] is True
    assert payment_body["currency"] == "KZT"
    assert payment_body["status"] == "PENDING"
    assert payment_body["amount"] == "25.75"
    assert "DEMO ONLY" in payment_body["notice"]
    payment_id = UUID(payment_body["payment_id"])

    # Retrying payment creation for the same pending order returns the same row.
    repeated_start = await client.post(
        f"{API}/orders/{order_id}/payments/simulate", headers=bearer(buyer_token)
    )
    assert repeated_start.status_code == 201, repeated_start.text
    assert repeated_start.json()["payment_id"] == str(payment_id)

    success = await client.post(
        f"{API}/orders/payments/{payment_id}/simulate-result",
        json={"outcome": "success"},
        headers=bearer(buyer_token),
    )
    assert success.status_code == 200, success.text
    assert success.json()["status"] == "SUCCEEDED"

    repeated = await client.post(
        f"{API}/orders/payments/{payment_id}/simulate-result",
        json={"outcome": "success"},
        headers=bearer(buyer_token),
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["id"] == str(payment_id)

    order = await db.get(Order, order_id)
    assert order is not None and order.status == OrderStatus.PAID
    assert await _count_for_order(db, Ticket, order_id) == 1
    assert await _count_for_order(db, Payment, order_id) == 1
    hold = await db.scalar(select(InventoryHold).where(InventoryHold.order_id == order_id))
    assert hold is not None and hold.status == HoldStatus.CONSUMED
    payment = await db.get(Payment, payment_id)
    assert payment is not None and payment.status == PaymentStatus.SUCCEEDED
    assert payment.is_simulated is True


@pytest.mark.parametrize(
    ("outcome", "expected_payment_status"),
    [("failed", PaymentStatus.FAILED), ("abandoned", PaymentStatus.CANCELLED)],
)
async def test_failed_or_abandoned_payment_releases_inventory(
    client, app, db, outcome, expected_payment_status
):
    _owner, _owner_token, event, ticket_type = await _make_paid_event(
        client, app, db, email=f"{outcome}-owner@example.com", capacity=1
    )
    buyer, buyer_token, order_response = await _create_paid_order(
        client, app, db, event, ticket_type, email=f"{outcome}-buyer@example.com"
    )
    assert order_response.status_code == 201, order_response.text
    order_id = UUID(order_response.json()["id"])

    started = await client.post(
        f"{API}/orders/{order_id}/payments/simulate", headers=bearer(buyer_token)
    )
    assert started.status_code == 201, started.text
    payment_id = started.json()["payment_id"]

    result = await client.post(
        f"{API}/orders/payments/{payment_id}/simulate-result",
        json={"outcome": outcome},
        headers=bearer(buyer_token),
    )
    assert result.status_code == 200, result.text
    assert result.json()["status"] == expected_payment_status.value

    order = await db.get(Order, order_id)
    assert order is not None and order.status == OrderStatus.CANCELLED
    assert await _count_for_order(db, Ticket, order_id) == 0
    assert await _count_for_order(db, Payment, order_id) == 1
    hold = await db.scalar(select(InventoryHold).where(InventoryHold.order_id == order_id))
    assert hold is not None and hold.status == HoldStatus.RELEASED
