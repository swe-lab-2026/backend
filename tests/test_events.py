import asyncio
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import select

from app.db.models import AuditLog, InventoryHold, Order, TicketType
from tests.conftest import API, access_token_for, bearer, create_user

OWNER_EMAIL = "owner@example.com"
STRANGER_EMAIL = "stranger@example.com"
BUYER_EMAIL = "buyer@example.com"

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _make_organizer(client, app, db, *, email: str = OWNER_EMAIL):
    user = await create_user(db, email=email)
    token = access_token_for(app, user)
    resp = await client.post(
        f"{API}/organizers/me",
        json={"display_name": "Test Org", "terms_accepted": True},
        headers=bearer(token),
    )
    assert resp.status_code == 201, resp.text
    return user, token


def _event_payload(**overrides):
    now = _now()
    payload = {
        "title": "Test Event",
        "description": "A test event description",
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


def _ticket_type_payload(**overrides):
    payload = {
        "name": "General",
        "price": "50.00",
        "currency": "KZT",
        "capacity": 50,
        "min_per_order": 1,
        "max_per_order": 10,
    }
    payload.update(overrides)
    return payload


async def _create_event(client, token, **overrides):
    resp = await client.post(
        f"{API}/events", json=_event_payload(**overrides), headers=bearer(token)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _add_ticket_type(client, token, event_id, **overrides):
    resp = await client.post(
        f"{API}/events/{event_id}/ticket-types",
        json=_ticket_type_payload(**overrides),
        headers=bearer(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _publish(client, token, event_id):
    return await client.post(f"{API}/events/{event_id}/publish", headers=bearer(token))


async def _audit_actions(db, action: str):
    stmt = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id.asc())
    return (await db.execute(stmt)).scalars().all()


# 1. Organizer creates an event ----------------------------------------------
async def test_organizer_creates_event(client, app, db):
    user, token = await _make_organizer(client, app, db)
    body = await _create_event(client, token)
    assert body["status"] == "DRAFT"
    assert body["owner_user_id"] == str(user.id)
    assert body["visibility"] == "PUBLIC"


# 2. Organizer edits their own event -----------------------------------------
async def test_organizer_edits_own_event(client, app, db):
    _, token = await _make_organizer(client, app, db)
    event = await _create_event(client, token)

    resp = await client.patch(
        f"{API}/events/{event['id']}", json={"title": "Renamed"}, headers=bearer(token)
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "Renamed"


# 3. Organizer cannot edit a stranger's event --------------------------------
async def test_cannot_edit_strangers_event(client, app, db):
    _, token = await _make_organizer(client, app, db)
    event = await _create_event(client, token)

    stranger = await create_user(db, email=STRANGER_EMAIL)
    resp = await client.patch(
        f"{API}/events/{event['id']}",
        json={"title": "Hijacked"},
        headers=bearer(access_token_for(app, stranger)),
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "INSUFFICIENT_EVENT_PERMISSIONS"


# 4. Event missing required data cannot be published --------------------------
async def test_publish_missing_required_data(client, app, db):
    _, token = await _make_organizer(client, app, db)
    resp = await client.post(
        f"{API}/events",
        json=_event_payload(description=None, category=None, venue_name=None, capacity=None),
        headers=bearer(token),
    )
    assert resp.status_code == 201

    publish = await _publish(client, token, resp.json()["id"])
    assert publish.status_code == 422
    body = publish.json()["error"]
    assert body["code"] == "EVENT_NOT_PUBLISHABLE"
    assert "description is required" in body["details"]["errors"]
    assert "venue_name is required" in body["details"]["errors"]


# 5. Event without a ticket type cannot be published --------------------------
async def test_publish_without_ticket_type(client, app, db):
    _, token = await _make_organizer(client, app, db)
    event = await _create_event(client, token)

    publish = await _publish(client, token, event["id"])
    assert publish.status_code == 422
    assert publish.json()["error"]["code"] == "EVENT_NOT_PUBLISHABLE"


# 6. Valid event publishes successfully ---------------------------------------
async def test_publish_success(client, app, db):
    _, token = await _make_organizer(client, app, db)
    event = await _create_event(client, token)
    await _add_ticket_type(client, token, event["id"])

    publish = await _publish(client, token, event["id"])
    assert publish.status_code == 200
    assert publish.json()["status"] == "PUBLISHED"
    assert publish.json()["published_at"] is not None


# 7. Duplication creates a new draft and copies configuration -----------------
async def test_duplicate_creates_draft_copy(client, app, db):
    _, token = await _make_organizer(client, app, db)
    event = await _create_event(client, token, visibility="PRIVATE", category="Conference")
    await _add_ticket_type(client, token, event["id"], name="VIP", price="100.00")

    resp = await client.post(f"{API}/events/{event['id']}/duplicate", headers=bearer(token))
    assert resp.status_code == 201
    copy = resp.json()
    assert copy["id"] != event["id"]
    assert copy["status"] == "DRAFT"
    assert copy["visibility"] == "PRIVATE"
    assert copy["category"] == "Conference"
    assert copy["title"] == event["title"]

    types = await client.get(
        f"{API}/events/{copy['id']}/ticket-types", headers=bearer(token)
    )
    assert types.status_code == 200
    names = [t["name"] for t in types.json()]
    assert "VIP" in names


# 8. Duplication does not copy orders, registrations or attendees -------------
async def test_duplicate_does_not_copy_sales(client, app, db):
    _, token = await _make_organizer(client, app, db)
    buyer = await create_user(db, email=BUYER_EMAIL)
    event = await _create_event(client, token)
    ticket_type = await _add_ticket_type(
        client, token, event["id"], name="Free", price="0", capacity=10
    )
    assert (await _publish(client, token, event["id"])).status_code == 200

    purchase = await client.post(
        f"{API}/events/{event['id']}/ticket-types/{ticket_type['id']}/purchase",
        json={"quantity": 1},
        headers=bearer(access_token_for(app, buyer)),
    )
    assert purchase.status_code == 201

    dup = await client.post(f"{API}/events/{event['id']}/duplicate", headers=bearer(token))
    copy_id = UUID(dup.json()["id"])

    order_count = len(
        (await db.execute(select(Order).where(Order.event_id == copy_id))).scalars().all()
    )
    hold_count = len(
        (
            await db.execute(select(InventoryHold).where(InventoryHold.event_id == copy_id))
        ).scalars().all()
    )
    assert order_count == 0
    assert hold_count == 0

    # The original still has its sale intact.
    stmt = select(Order).where(Order.event_id == UUID(event["id"]))
    assert (await db.execute(stmt)).scalars().first() is not None


# 9. Sales are closed before the start and after the end of the sales window --
async def test_sales_window_closed(client, app, db):
    _, token = await _make_organizer(client, app, db)
    buyer = await create_user(db, email=BUYER_EMAIL)
    buyer_token = access_token_for(app, buyer)
    now = _now()

    future_event = await _create_event(client, token, title="Future")
    future_tt = await _add_ticket_type(
        client,
        token,
        future_event["id"],
        sales_starts_at=(now + timedelta(hours=1)).isoformat(),
        sales_ends_at=(now + timedelta(hours=2)).isoformat(),
    )
    assert (await _publish(client, token, future_event["id"])).status_code == 200
    resp = await client.post(
        f"{API}/events/{future_event['id']}/ticket-types/{future_tt['id']}/purchase",
        json={"quantity": 1},
        headers=bearer(buyer_token),
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "SALES_NOT_OPEN"

    past_event = await _create_event(client, token, title="Past")
    past_tt = await _add_ticket_type(
        client,
        token,
        past_event["id"],
        sales_starts_at=(now - timedelta(hours=2)).isoformat(),
        sales_ends_at=(now - timedelta(hours=1)).isoformat(),
    )
    assert (await _publish(client, token, past_event["id"])).status_code == 200
    resp = await client.post(
        f"{API}/events/{past_event['id']}/ticket-types/{past_tt['id']}/purchase",
        json={"quantity": 1},
        headers=bearer(buyer_token),
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "SALES_CLOSED"


# 10. Sale is impossible once the ticket type is exhausted --------------------
async def test_ticket_type_sold_out(client, app, db):
    _, token = await _make_organizer(client, app, db)
    buyer = await create_user(db, email=BUYER_EMAIL)
    buyer_token = access_token_for(app, buyer)

    event = await _create_event(client, token)
    ticket_type = await _add_ticket_type(
        client, token, event["id"], name="Limited", price="50.00", capacity=2, max_per_order=2
    )
    assert (await _publish(client, token, event["id"])).status_code == 200

    first = await client.post(
        f"{API}/events/{event['id']}/ticket-types/{ticket_type['id']}/purchase",
        json={"quantity": 2},
        headers=bearer(buyer_token),
    )
    assert first.status_code == 201

    second = await client.post(
        f"{API}/events/{event['id']}/ticket-types/{ticket_type['id']}/purchase",
        json={"quantity": 1},
        headers=bearer(buyer_token),
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "TICKET_SOLD_OUT"


# 11. Sale is impossible once the event capacity is reached -------------------
async def test_event_capacity_reached(client, app, db):
    _, token = await _make_organizer(client, app, db)
    buyer = await create_user(db, email=BUYER_EMAIL)
    buyer_token = access_token_for(app, buyer)

    event = await _create_event(client, token, capacity=3)
    ticket_type = await _add_ticket_type(
        client, token, event["id"], name="GA", price="50.00", capacity=10, max_per_order=10
    )
    assert (await _publish(client, token, event["id"])).status_code == 200

    first = await client.post(
        f"{API}/events/{event['id']}/ticket-types/{ticket_type['id']}/purchase",
        json={"quantity": 3},
        headers=bearer(buyer_token),
    )
    assert first.status_code == 201

    second = await client.post(
        f"{API}/events/{event['id']}/ticket-types/{ticket_type['id']}/purchase",
        json={"quantity": 1},
        headers=bearer(buyer_token),
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "EVENT_SOLD_OUT"


# 12. Cancelled event rejects new registrations and sales ----------------------
async def test_cancelled_event_rejects_sales(client, app, db):
    _, token = await _make_organizer(client, app, db)
    buyer = await create_user(db, email=BUYER_EMAIL)
    buyer_token = access_token_for(app, buyer)

    event = await _create_event(client, token)
    ticket_type = await _add_ticket_type(client, token, event["id"])
    assert (await _publish(client, token, event["id"])).status_code == 200
    cancel = await client.post(f"{API}/events/{event['id']}/cancel", headers=bearer(token))
    assert cancel.status_code == 200

    purchase = await client.post(
        f"{API}/events/{event['id']}/ticket-types/{ticket_type['id']}/purchase",
        json={"quantity": 1},
        headers=bearer(buyer_token),
    )
    assert purchase.status_code == 409
    assert purchase.json()["error"]["code"] == "EVENT_CANCELLED"

    hold = await client.post(
        f"{API}/events/{event['id']}/ticket-types/{ticket_type['id']}/holds",
        json={"quantity": 1},
        headers=bearer(buyer_token),
    )
    assert hold.status_code == 409
    assert hold.json()["error"]["code"] == "EVENT_CANCELLED"


# 13. Hidden ticket type is not shown to buyers but stays in the database ------
async def test_hidden_ticket_type(client, app, db):
    _, token = await _make_organizer(client, app, db)
    buyer = await create_user(db, email=BUYER_EMAIL)
    buyer_token = access_token_for(app, buyer)

    event = await _create_event(client, token)
    visible = await _add_ticket_type(client, token, event["id"], name="Public")
    secret = await _add_ticket_type(client, token, event["id"], name="Secret")
    assert (await _publish(client, token, event["id"])).status_code == 200

    hide = await client.delete(
        f"{API}/events/{event['id']}/ticket-types/{secret['id']}", headers=bearer(token)
    )
    assert hide.status_code == 204

    owner_view = await client.get(
        f"{API}/events/{event['id']}/ticket-types", headers=bearer(token)
    )
    owner_names = {t["name"]: t for t in owner_view.json()}
    assert "Secret" in owner_names
    assert owner_names["Secret"]["is_hidden"] is True

    buyer_view = await client.get(
        f"{API}/events/{event['id']}/ticket-types", headers=bearer(buyer_token)
    )
    buyer_names = {t["name"] for t in buyer_view.json()}
    assert visible["name"] in buyer_names
    assert "Secret" not in buyer_names

    stmt = select(TicketType).where(TicketType.id == UUID(secret["id"]))
    row = (await db.execute(stmt)).scalar_one()
    assert row.is_hidden is True


# 14. Free ticket has a zero price --------------------------------------------
async def test_free_ticket_zero_price(client, app, db):
    _, token = await _make_organizer(client, app, db)
    event = await _create_event(client, token)
    ticket_type = await _add_ticket_type(client, token, event["id"], name="Free", price="0")
    assert ticket_type["is_free"] is True
    assert ticket_type["price"] == "0.00"


# 15. Paid ticket cannot have a zero or negative price ------------------------
async def test_paid_ticket_price_validation(client, app, db):
    _, token = await _make_organizer(client, app, db)
    event = await _create_event(client, token)

    resp = await client.post(
        f"{API}/events/{event['id']}/ticket-types",
        json=_ticket_type_payload(name="Negative", price="-1.00"),
        headers=bearer(token),
    )
    assert resp.status_code == 422

    paid = await _add_ticket_type(client, token, event["id"], name="Paid", price="50.00")
    assert paid["is_free"] is False
    assert paid["price"] == "50.00"


# 16. Capacity cannot be reduced below sold or reserved quantities -------------
async def test_capacity_cannot_be_reduced_below_sold(client, app, db):
    _, token = await _make_organizer(client, app, db)
    buyer = await create_user(db, email=BUYER_EMAIL)
    buyer_token = access_token_for(app, buyer)

    event = await _create_event(client, token, capacity=10)
    ticket_type = await _add_ticket_type(
        client, token, event["id"], name="Free", price="0", capacity=10
    )
    assert (await _publish(client, token, event["id"])).status_code == 200

    purchase = await client.post(
        f"{API}/events/{event['id']}/ticket-types/{ticket_type['id']}/purchase",
        json={"quantity": 3},
        headers=bearer(buyer_token),
    )
    assert purchase.status_code == 201

    tt_patch = await client.patch(
        f"{API}/events/{event['id']}/ticket-types/{ticket_type['id']}",
        json={"capacity": 2},
        headers=bearer(token),
    )
    assert tt_patch.status_code == 409
    assert tt_patch.json()["error"]["code"] == "CAPACITY_BELOW_SOLD"

    event_patch = await client.patch(
        f"{API}/events/{event['id']}", json={"capacity": 2}, headers=bearer(token)
    )
    assert event_patch.status_code == 409
    assert event_patch.json()["error"]["code"] == "CAPACITY_BELOW_SOLD"


# 17. Price, capacity and status changes are audited ---------------------------
async def test_price_capacity_status_changes_audited(client, app, db):
    _, token = await _make_organizer(client, app, db)
    event = await _create_event(client, token)
    ticket_type = await _add_ticket_type(client, token, event["id"], name="Paid", price="50.00")

    await client.patch(
        f"{API}/events/{event['id']}/ticket-types/{ticket_type['id']}",
        json={"price": "75.00"},
        headers=bearer(token),
    )
    price_entries = await _audit_actions(db, "ticket_type.updated")
    assert any(
        e.before_data and e.before_data.get("price") == "50.00" and e.after_data
        and e.after_data.get("price") == "75.00"
        for e in price_entries
    )

    await client.patch(
        f"{API}/events/{event['id']}", json={"capacity": 150}, headers=bearer(token)
    )
    capacity_entries = await _audit_actions(db, "event.updated")
    assert any(
        e.before_data and e.before_data.get("capacity") == 100 and e.after_data
        and e.after_data.get("capacity") == 150
        for e in capacity_entries
    )

    assert (await _publish(client, token, event["id"])).status_code == 200
    publish_entries = await _audit_actions(db, "event.published")
    assert len(publish_entries) == 1


# 18. Parallel requests do not oversell ---------------------------------------
@pytest.mark.skipif(not TEST_DATABASE_URL, reason="requires PostgreSQL row locking")
async def test_parallel_holds_do_not_oversell(client, app, db):
    _, token = await _make_organizer(client, app, db)
    buyer = await create_user(db, email=BUYER_EMAIL)
    buyer_token = access_token_for(app, buyer)

    event = await _create_event(client, token)
    ticket_type = await _add_ticket_type(
        client, token, event["id"], name="Hot", price="0", capacity=1, max_per_order=1
    )
    assert (await _publish(client, token, event["id"])).status_code == 200

    url = f"{API}/events/{event['id']}/ticket-types/{ticket_type['id']}/holds"

    async def _hold() -> int:
        resp = await client.post(url, json={"quantity": 1}, headers=bearer(buyer_token))
        return resp.status_code

    results = await asyncio.gather(_hold(), _hold())
    assert sorted(results) == [201, 409]

    active = await db.execute(
        select(InventoryHold).where(
            InventoryHold.ticket_type_id == UUID(ticket_type["id"])
        )
    )
    holds = active.scalars().all()
    assert sum(h.quantity for h in holds) <= 1


# 19. private and unlisted events are restricted on public routes --------------
async def test_visibility_restrictions(client, app, db):
    _, token = await _make_organizer(client, app, db)
    stranger = await create_user(db, email=STRANGER_EMAIL)
    stranger_token = access_token_for(app, stranger)

    private = await _create_event(client, token, title="Private", visibility="PRIVATE")
    await _add_ticket_type(client, token, private["id"])
    assert (await _publish(client, token, private["id"])).status_code == 200

    unlisted = await _create_event(client, token, title="Unlisted", visibility="UNLISTED")
    await _add_ticket_type(client, token, unlisted["id"])
    assert (await _publish(client, token, unlisted["id"])).status_code == 200

    public = await _create_event(client, token, title="Public", visibility="PUBLIC")
    await _add_ticket_type(client, token, public["id"])
    assert (await _publish(client, token, public["id"])).status_code == 200

    # Private event is not reachable by a stranger via direct link.
    resp = await client.get(f"{API}/events/{private['id']}", headers=bearer(stranger_token))
    assert resp.status_code == 404

    # Unlisted event is reachable by direct link but absent from the catalog.
    resp = await client.get(f"{API}/events/{unlisted['id']}", headers=bearer(stranger_token))
    assert resp.status_code == 200

    catalog = await client.get(f"{API}/events/catalog")
    assert catalog.status_code == 200
    catalog_titles = {e["title"] for e in catalog.json()}
    assert "Public" in catalog_titles
    assert "Unlisted" not in catalog_titles
    assert "Private" not in catalog_titles


# 20. Invalid requests return expected status codes ----------------------------
async def test_invalid_requests_return_errors(client, app, db):
    _, token = await _make_organizer(client, app, db)

    resp = await client.post(f"{API}/events", json={"title": "X"}, headers=bearer(token))
    assert resp.status_code == 422

    resp = await client.get(f"{API}/events/{UUID(int=0)}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "EVENT_NOT_FOUND"

    event = await _create_event(client, token)
    resp = await client.delete(f"{API}/events/{event['id']}", headers=bearer(token))
    assert resp.status_code == 204

    resp = await client.get(f"{API}/events/{event['id']}", headers=bearer(token))
    assert resp.status_code == 404
