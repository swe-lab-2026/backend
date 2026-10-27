from datetime import datetime, timedelta, timezone
from uuid import UUID

from app.db.models import Order, OrderNotification, Ticket
from tests.conftest import API, access_token_for, bearer, create_user

OWNER_EMAIL = "ticket-owner@example.com"
BUYER_EMAIL = "ticket-buyer@example.com"
STRANGER_EMAIL = "ticket-stranger@example.com"


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _setup_event(client, app, db, *, price: str = "0", capacity: int = 20):
    owner = await create_user(db, email=OWNER_EMAIL)
    owner_token = access_token_for(app, owner)
    organizer = await client.post(
        f"{API}/organizers/me",
        json={"display_name": "Ticket Org", "terms_accepted": True},
        headers=bearer(owner_token),
    )
    assert organizer.status_code == 201, organizer.text

    now = _now()
    event = await client.post(
        f"{API}/events",
        json={
            "title": "Ticket Event",
            "description": "Admission ticket test",
            "category": "Music",
            "visibility": "PUBLIC",
            "venue_name": "Main Hall",
            "venue_address": "1 Abay Ave",
            "city": "Almaty",
            "timezone": "Asia/Almaty",
            "starts_at": (now + timedelta(days=2)).isoformat(),
            "ends_at": (now + timedelta(days=2, hours=3)).isoformat(),
            "registration_starts_at": (now - timedelta(days=1)).isoformat(),
            "registration_ends_at": (now + timedelta(days=1)).isoformat(),
            "capacity": 100,
        },
        headers=bearer(owner_token),
    )
    assert event.status_code == 201, event.text
    event_id = event.json()["id"]

    ticket_type = await client.post(
        f"{API}/events/{event_id}/ticket-types",
        json={
            "name": "General",
            "price": price,
            "currency": "KZT",
            "capacity": capacity,
            "min_per_order": 1,
            "max_per_order": 10,
        },
        headers=bearer(owner_token),
    )
    assert ticket_type.status_code == 201, ticket_type.text
    published = await client.post(
        f"{API}/events/{event_id}/publish", headers=bearer(owner_token)
    )
    assert published.status_code == 200, published.text

    buyer = await create_user(db, email=BUYER_EMAIL)
    buyer_token = access_token_for(app, buyer)
    return event_id, ticket_type.json()["id"], owner, owner_token, buyer, buyer_token


async def test_free_order_issues_tickets_pdf_and_one_email(client, app, db):
    event_id, ticket_type_id, owner, _, _, buyer_token = await _setup_event(client, app, db)

    response = await client.post(
        f"{API}/orders",
        json={"event_id": event_id, "items": [{"ticket_type_id": ticket_type_id, "quantity": 2}]},
        headers=bearer(buyer_token),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "CONFIRMED"
    assert body["is_free"] is True
    assert len(body["tickets"]) == 2
    payloads = {ticket["qr_payload"] for ticket in body["tickets"]}
    assert len(payloads) == 2
    assert all(payload.startswith("v1.") for payload in payloads)

    provider = app.state.email_provider
    confirmation = provider.last_order_confirmation_for(BUYER_EMAIL)
    assert confirmation is not None
    assert len(confirmation.tickets) == 2
    assert confirmation.event_name == "Ticket Event"

    public_id = body["tickets"][0]["public_id"]
    pdf = await client.get(
        f"{API}/tickets/{public_id}/pdf", headers=bearer(buyer_token)
    )
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF")
    assert len(pdf.content) > 1000

    notifications = (
        await db.execute(
            OrderNotification.__table__.select().where(
                OrderNotification.order_id == UUID(body["id"])
            )
        )
    ).all()
    assert len(notifications) == 1
    assert notifications[0].status == "SENT"

    # Retried confirmation must neither mint tickets nor send another email.
    again = await client.post(
        f"{API}/orders/{body['id']}/confirm", headers=bearer(buyer_token)
    )
    assert again.status_code == 200, again.text
    assert len(again.json()["tickets"]) == 2
    assert len(provider.order_confirmations()) == 1
    other_tickets = (
        await db.execute(
            Ticket.__table__.select().where(Ticket.status == "VALID")
        )
    ).all()
    assert len(other_tickets) == 2
    del owner


async def test_qr_validation_and_atomic_check_in(client, app, db):
    event_id, ticket_type_id, owner, owner_token, _, buyer_token = await _setup_event(
        client, app, db
    )
    order = await client.post(
        f"{API}/orders",
        json={"event_id": event_id, "items": [{"ticket_type_id": ticket_type_id, "quantity": 1}]},
        headers=bearer(buyer_token),
    )
    assert order.status_code == 201, order.text
    ticket = order.json()["tickets"][0]
    payload = ticket["qr_payload"]

    invalid = await client.post(
        f"{API}/events/{event_id}/tickets/validate",
        json={"qr_payload": payload + "x"},
        headers=bearer(owner_token),
    )
    assert invalid.status_code == 401
    assert invalid.json()["error"]["code"] == "INVALID_TICKET_QR"

    valid = await client.post(
        f"{API}/events/{event_id}/tickets/validate",
        json={"qr_payload": payload},
        headers=bearer(owner_token),
    )
    assert valid.status_code == 200, valid.text
    assert valid.json()["valid"] is True
    assert valid.json()["reason"] is None

    checked_in = await client.post(
        f"{API}/events/{event_id}/check-in",
        json={"qr_payload": payload},
        headers=bearer(owner_token),
    )
    assert checked_in.status_code == 200, checked_in.text
    assert checked_in.json()["status"] == "CHECKED_IN"
    assert checked_in.json()["checked_in_at"] is not None

    duplicate = await client.post(
        f"{API}/events/{event_id}/check-in",
        json={"qr_payload": payload},
        headers=bearer(owner_token),
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "TICKET_ALREADY_CHECKED_IN"

    after = await client.post(
        f"{API}/events/{event_id}/tickets/validate",
        json={"qr_payload": payload},
        headers=bearer(owner_token),
    )
    assert after.status_code == 200
    assert after.json()["valid"] is False
    assert after.json()["reason"] == "TICKET_CHECKED_IN"

    # Buyer cannot use the event check-in endpoint, but can read their ticket.
    forbidden = await client.post(
        f"{API}/events/{event_id}/check-in",
        json={"qr_payload": payload},
        headers=bearer(buyer_token),
    )
    assert forbidden.status_code == 403
    own = await client.get(
        f"{API}/tickets/{ticket['public_id']}", headers=bearer(buyer_token)
    )
    assert own.status_code == 200
    assert own.json()["status"] == "CHECKED_IN"
    del owner


async def test_ticket_access_is_owner_buyer_or_admin_only(client, app, db):
    event_id, ticket_type_id, _, _, _, buyer_token = await _setup_event(client, app, db)
    order = await client.post(
        f"{API}/orders",
        json={"event_id": event_id, "items": [{"ticket_type_id": ticket_type_id, "quantity": 1}]},
        headers=bearer(buyer_token),
    )
    public_id = order.json()["tickets"][0]["public_id"]

    stranger = await create_user(db, email=STRANGER_EMAIL)
    stranger_token = access_token_for(app, stranger)
    denied = await client.get(
        f"{API}/tickets/{public_id}", headers=bearer(stranger_token)
    )
    assert denied.status_code == 403


async def test_paid_order_only_issues_tickets_after_confirmation(client, app, db):
    event_id, ticket_type_id, _, _, buyer, buyer_token = await _setup_event(
        client, app, db, price="50.00"
    )
    created = await client.post(
        f"{API}/orders",
        json={"event_id": event_id, "items": [{"ticket_type_id": ticket_type_id, "quantity": 2}]},
        headers=bearer(buyer_token),
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "PENDING"
    assert body["tickets"] == []
    assert body["expires_at"] is not None
    assert app.state.email_provider.last_order_confirmation_for(BUYER_EMAIL) is None

    confirmed = await client.post(
        f"{API}/orders/{body['id']}/checkout", headers=bearer(buyer_token)
    )
    assert confirmed.status_code == 200, confirmed.text
    confirmed_body = confirmed.json()
    assert confirmed_body["status"] == "PAID"
    assert len(confirmed_body["tickets"]) == 2
    assert app.state.email_provider.last_order_confirmation_for(BUYER_EMAIL) is not None

    stored = await db.get(Order, UUID(body["id"]))
    assert stored is not None
    assert stored.buyer_user_id == buyer.id


async def test_cancelled_order_tickets_fail_validation(client, app, db):
    event_id, ticket_type_id, _, owner_token, _, buyer_token = await _setup_event(
        client, app, db
    )
    order = await client.post(
        f"{API}/orders",
        json={"event_id": event_id, "items": [{"ticket_type_id": ticket_type_id, "quantity": 1}]},
        headers=bearer(buyer_token),
    )
    body = order.json()
    payload = body["tickets"][0]["qr_payload"]

    cancelled = await client.post(
        f"{API}/orders/{body['id']}/cancel", headers=bearer(buyer_token)
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "CANCELLED"

    validated = await client.post(
        f"{API}/events/{event_id}/tickets/validate",
        json={"qr_payload": payload},
        headers=bearer(owner_token),
    )
    assert validated.status_code == 200
    assert validated.json()["valid"] is False
    assert validated.json()["reason"] == "TICKET_CANCELLED"


async def test_legacy_purchase_endpoint_issues_signed_tickets_and_email(client, app, db):
    event_id, ticket_type_id, _, owner_token, _, buyer_token = await _setup_event(
        client, app, db
    )
    purchase = await client.post(
        f"{API}/events/{event_id}/ticket-types/{ticket_type_id}/purchase",
        json={"quantity": 1},
        headers=bearer(buyer_token),
    )
    assert purchase.status_code == 201, purchase.text
    assert purchase.json()["status"] == "CONFIRMED"
    public_id = purchase.json()["tickets"][0]["public_id"]

    ticket = await client.get(f"{API}/tickets/{public_id}", headers=bearer(buyer_token))
    assert ticket.status_code == 200, ticket.text
    payload = ticket.json()["qr_payload"]
    assert payload.startswith("v1.")

    validated = await client.post(
        f"{API}/events/{event_id}/tickets/validate",
        json={"qr_payload": payload},
        headers=bearer(owner_token),
    )
    assert validated.status_code == 200
    assert validated.json()["valid"] is True
    assert app.state.email_provider.last_order_confirmation_for(BUYER_EMAIL) is not None
