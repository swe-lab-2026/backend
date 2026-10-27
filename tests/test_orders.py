"""Regression tests for checkout inventory reservations and free registration."""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.db.models import (
    HoldStatus,
    InventoryHold,
    Order,
    OrderItem,
    OrderStatus,
    Ticket,
)
from tests.conftest import API, access_token_for, bearer, create_user

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event_payload(**overrides):
    now = _now()
    payload = {
        "title": "Order Test Event",
        "description": "A test event for checkout",
        "category": "Music",
        "visibility": "PUBLIC",
        "venue_name": "Main Hall",
        "timezone": "UTC",
        "starts_at": (now + timedelta(days=2)).isoformat(),
        "ends_at": (now + timedelta(days=2, hours=3)).isoformat(),
        "registration_starts_at": (now - timedelta(days=1)).isoformat(),
        "registration_ends_at": (now + timedelta(days=1)).isoformat(),
        "capacity": 100,
    }
    payload.update(overrides)
    return payload


def _ticket_payload(**overrides):
    payload = {
        "name": "General Admission",
        "price": "25.00",
        "currency": "KZT",
        "capacity": 10,
        "min_per_order": 1,
        "max_per_order": 5,
    }
    payload.update(overrides)
    return payload


async def _make_owner(client, app, db):
    owner = await create_user(db, email="orders-owner@example.com")
    token = access_token_for(app, owner)
    profile = await client.post(
        f"{API}/organizers/me",
        json={"display_name": "Order Test Organizer", "terms_accepted": True},
        headers=bearer(token),
    )
    assert profile.status_code == 201, profile.text
    return owner, token


async def _make_event(client, owner_token, **event_overrides):
    event_response = await client.post(
        f"{API}/events",
        json=_event_payload(**event_overrides),
        headers=bearer(owner_token),
    )
    assert event_response.status_code == 201, event_response.text
    event = event_response.json()

    ticket_response = await client.post(
        f"{API}/events/{event['id']}/ticket-types",
        json=_ticket_payload(),
        headers=bearer(owner_token),
    )
    assert ticket_response.status_code == 201, ticket_response.text
    ticket_type = ticket_response.json()

    publish_response = await client.post(
        f"{API}/events/{event['id']}/publish", headers=bearer(owner_token)
    )
    assert publish_response.status_code == 200, publish_response.text
    return event, ticket_type


async def test_free_order_is_confirmed_and_visible_to_organizer(client, app, db):
    _owner, owner_token = await _make_owner(client, app, db)
    event, ticket_type = await _make_event(
        client,
        owner_token,
        capacity=10,
    )
    # Change the ticket type to free before checkout.
    type_update = await client.patch(
        f"{API}/events/{event['id']}/ticket-types/{ticket_type['id']}",
        json={"price": "0.00", "max_per_order": 3},
        headers=bearer(owner_token),
    )
    assert type_update.status_code == 200, type_update.text

    buyer = await create_user(
        db,
        email="free-buyer@example.com",
        first_name="Free",
        last_name="Buyer",
    )
    buyer_token = access_token_for(app, buyer)
    response = await client.post(
        f"{API}/orders",
        json={
            "event_id": event["id"],
            "items": [{"ticket_type_id": ticket_type["id"], "quantity": 2}],
        },
        headers=bearer(buyer_token),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "PAID"
    assert body["currency"] == "KZT"
    assert float(body["total_amount"]) == 0
    assert body["expires_at"] is None
    assert body["items"][0]["quantity"] == 2

    order = await db.get(Order, UUID(body["id"]))
    assert order is not None
    assert order.status == OrderStatus.PAID
    assert order.total_amount == 0
    assert order.paid_at is not None

    item_count = await db.scalar(
        select(func.count(OrderItem.id)).where(OrderItem.order_id == order.id)
    )
    ticket_count = await db.scalar(
        select(func.count(Ticket.id))
        .join(OrderItem, Ticket.order_item_id == OrderItem.id)
        .where(OrderItem.order_id == order.id)
    )
    hold_count = await db.scalar(
        select(func.count(InventoryHold.id)).where(InventoryHold.order_id == order.id)
    )
    assert item_count == 2
    assert ticket_count == 2
    assert hold_count == 0

    attendee_response = await client.get(
        f"{API}/events/{event['id']}/attendees", headers=bearer(owner_token)
    )
    assert attendee_response.status_code == 200, attendee_response.text
    attendees = attendee_response.json()
    assert len(attendees) == 1
    assert attendees[0]["email"] == buyer.email
    assert attendees[0]["full_name"] == "Free Buyer"
    assert attendees[0]["ticket_count"] == 2
    assert float(attendees[0]["total_amount"]) == 0

    forbidden = await client.get(
        f"{API}/events/{event['id']}/attendees", headers=bearer(buyer_token)
    )
    assert forbidden.status_code == 403


async def test_paid_order_reserves_inventory_and_expiration_releases_it(client, app, db):
    _owner, owner_token = await _make_owner(client, app, db)
    event, ticket_type = await _make_event(
        client,
        owner_token,
        capacity=1,
    )
    await client.patch(
        f"{API}/events/{event['id']}/ticket-types/{ticket_type['id']}",
        json={"capacity": 1, "max_per_order": 1},
        headers=bearer(owner_token),
    )

    buyer = await create_user(db, email="paid-buyer@example.com")
    response = await client.post(
        f"{API}/orders",
        json={
            "event_id": event["id"],
            "items": [{"ticket_type_id": ticket_type["id"], "quantity": 1}],
        },
        headers=bearer(access_token_for(app, buyer)),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "PENDING"
    assert body["expires_at"] is not None

    order = await db.get(Order, UUID(body["id"]))
    assert order is not None
    assert order.expires_at is not None
    assert order.expires_at > _now()
    hold = await db.scalar(select(InventoryHold).where(InventoryHold.order_id == order.id))
    assert hold is not None
    assert hold.status == HoldStatus.ACTIVE
    assert hold.expires_at == order.expires_at

    # Simulate the ten-minute checkout timeout without sleeping.
    expired_at = _now() - timedelta(seconds=1)
    order.expires_at = expired_at
    hold.expires_at = expired_at
    await db.commit()

    read_response = await client.get(
        f"{API}/orders/{body['id']}",
        headers=bearer(access_token_for(app, buyer)),
    )
    assert read_response.status_code == 200, read_response.text
    assert read_response.json()["status"] == "EXPIRED"
    await db.refresh(hold)
    await db.refresh(order)
    assert hold.status == HoldStatus.EXPIRED
    assert order.status == OrderStatus.EXPIRED

    second_buyer = await create_user(db, email="paid-buyer-2@example.com")
    retry = await client.post(
        f"{API}/orders",
        json={
            "event_id": event["id"],
            "items": [{"ticket_type_id": ticket_type["id"], "quantity": 1}],
        },
        headers=bearer(access_token_for(app, second_buyer)),
    )
    assert retry.status_code == 201, retry.text


async def test_duplicate_lines_cannot_bypass_per_order_limit(client, app, db):
    _owner, owner_token = await _make_owner(client, app, db)
    event, ticket_type = await _make_event(client, owner_token)
    await client.patch(
        f"{API}/events/{event['id']}/ticket-types/{ticket_type['id']}",
        json={"max_per_order": 3},
        headers=bearer(owner_token),
    )
    buyer = await create_user(db, email="limit-buyer@example.com")

    response = await client.post(
        f"{API}/orders",
        json={
            "event_id": event["id"],
            "items": [
                {"ticket_type_id": ticket_type["id"], "quantity": 2},
                {"ticket_type_id": ticket_type["id"], "quantity": 2},
            ],
        },
        headers=bearer(access_token_for(app, buyer)),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "ORDER_LIMIT_EXCEEDED"
    assert "allows" in response.json()["error"]["message"]


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="row-lock concurrency test requires PostgreSQL")
async def test_concurrent_free_orders_cannot_sell_last_ticket_twice(client, app, db):
    _owner, owner_token = await _make_owner(client, app, db)
    event, ticket_type = await _make_event(client, owner_token, capacity=1)
    await client.patch(
        f"{API}/events/{event['id']}/ticket-types/{ticket_type['id']}",
        json={"price": "0.00", "capacity": 1, "max_per_order": 1},
        headers=bearer(owner_token),
    )

    buyers = [
        await create_user(db, email="race-buyer-1@example.com"),
        await create_user(db, email="race-buyer-2@example.com"),
    ]

    async def order_for(user):
        return await client.post(
            f"{API}/orders",
            json={
                "event_id": event["id"],
                "items": [{"ticket_type_id": ticket_type["id"], "quantity": 1}],
            },
            headers=bearer(access_token_for(app, user)),
        )

    responses = await asyncio.gather(*(order_for(buyer) for buyer in buyers))
    assert sorted(response.status_code for response in responses) == [201, 409]

    paid_order_count = await db.scalar(
        select(func.count(Order.id)).where(
            Order.event_id == UUID(event["id"]), Order.status == OrderStatus.PAID
        )
    )
    assert paid_order_count == 1
