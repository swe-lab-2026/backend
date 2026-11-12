"""SCRUM-13: manual search, manual check-in, reversal, stats, and QR edges."""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.db.models import (
    Attendee,
    Campaign,
    CampaignStatus,
    CheckinAction,
    CheckinRecord,
    DiscountType,
    EventStaff,
    EventStaffRole,
    OrderItem,
    Ticket,
    TicketStatus,
)
from tests.conftest import API, access_token_for, bearer, create_user
from tests.test_tickets import _setup_event

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
needs_pg = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="row-lock concurrency test requires PostgreSQL"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _create_published_event(
    client, owner_token: str, *, title: str, capacity: int = 20
) -> tuple[str, str]:
    now = _now()
    event = await client.post(
        f"{API}/events",
        json={
            "title": title,
            "description": "Second event for cross-event checks",
            "category": "Music",
            "visibility": "PUBLIC",
            "venue_name": "Second Hall",
            "city": "Almaty",
            "timezone": "Asia/Almaty",
            "starts_at": (now + timedelta(days=3)).isoformat(),
            "ends_at": (now + timedelta(days=3, hours=3)).isoformat(),
            "registration_starts_at": (now - timedelta(days=1)).isoformat(),
            "registration_ends_at": (now + timedelta(days=2)).isoformat(),
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
            "price": "0",
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
    return event_id, ticket_type.json()["id"]


async def _buy(
    client,
    token: str,
    event_id: str,
    ticket_type_id: str,
    *,
    quantity: int = 1,
) -> dict:
    response = await client.post(
        f"{API}/orders",
        json={
            "event_id": event_id,
            "items": [{"ticket_type_id": ticket_type_id, "quantity": quantity}],
        },
        headers=bearer(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _assign_staff(
    db, *, event_id: str, user_id: UUID, role: EventStaffRole
) -> None:
    db.add(
        EventStaff(
            event_id=UUID(event_id),
            user_id=user_id,
            role=role,
            permissions={},
        )
    )
    await db.commit()


async def _checkin_records(db, ticket_id: UUID) -> list[CheckinRecord]:
    result = await db.execute(
        select(CheckinRecord)
        .where(CheckinRecord.ticket_id == ticket_id)
        .order_by(CheckinRecord.created_at.asc(), CheckinRecord.id.asc())
    )
    return list(result.scalars().all())


async def test_manual_search_is_event_scoped_and_supports_identifiers(client, app, db):
    event_a, type_a, _owner, owner_token, buyer, buyer_token = await _setup_event(
        client, app, db
    )
    event_b, type_b = await _create_published_event(
        client, owner_token, title="Other Event"
    )
    order_a = await _buy(client, buyer_token, event_a, type_a, quantity=2)
    order_b = await _buy(client, buyer_token, event_b, type_b, quantity=1)

    by_email = await client.get(
        f"{API}/events/{event_a}/tickets/search",
        params={"q": buyer.email},
        headers=bearer(owner_token),
    )
    assert by_email.status_code == 200, by_email.text
    rows = by_email.json()
    assert len(rows) == 2
    assert {row["event_id"] for row in rows} == {event_a}
    assert {row["status"] for row in rows} == {"VALID"}
    assert all(row["checked_in_at"] is None for row in rows)

    public_id = order_a["tickets"][0]["public_id"]
    by_public_id = await client.get(
        f"{API}/events/{event_a}/tickets/search",
        params={"q": public_id[:8]},
        headers=bearer(owner_token),
    )
    assert by_public_id.status_code == 200
    assert [row["public_id"] for row in by_public_id.json()] == [public_id]

    by_order = await client.get(
        f"{API}/events/{event_a}/tickets/search",
        params={"q": str(order_a["order_number"])},
        headers=bearer(owner_token),
    )
    assert by_order.status_code == 200
    assert len(by_order.json()) == 2

    first_page = await client.get(
        f"{API}/events/{event_a}/tickets/search",
        params={"q": buyer.email, "limit": 1, "offset": 0},
        headers=bearer(owner_token),
    )
    second_page = await client.get(
        f"{API}/events/{event_a}/tickets/search",
        params={"q": buyer.email, "limit": 1, "offset": 1},
        headers=bearer(owner_token),
    )
    assert len(first_page.json()) == 1
    assert len(second_page.json()) == 1
    assert first_page.json()[0]["public_id"] != second_page.json()[0]["public_id"]

    # Event B's ticket exists, but searching event A must never return it.
    event_b_ticket = order_b["tickets"][0]["public_id"]
    leaked = await client.get(
        f"{API}/events/{event_a}/tickets/search",
        params={"q": event_b_ticket},
        headers=bearer(owner_token),
    )
    assert leaked.status_code == 200
    assert leaked.json() == []

    forbidden = await client.get(
        f"{API}/events/{event_a}/tickets/search",
        params={"q": buyer.email},
        headers=bearer(buyer_token),
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "INSUFFICIENT_EVENT_PERMISSIONS"


async def test_search_trims_query_and_matches_buyer_email_fallback(client, app, db):
    event_id, ticket_type_id, _owner, owner_token, buyer, buyer_token = (
        await _setup_event(client, app, db)
    )
    order = await _buy(client, buyer_token, event_id, ticket_type_id, quantity=2)
    first_ticket, second_ticket = order["tickets"]

    first_row = await db.get(Ticket, UUID(first_ticket["ticket_id"]))
    second_row = await db.get(Ticket, UUID(second_ticket["ticket_id"]))
    assert first_row is not None and second_row is not None
    first_item = await db.get(OrderItem, first_row.order_item_id)
    second_item = await db.get(OrderItem, second_row.order_item_id)
    assert first_item is not None and second_item is not None
    attendee = await db.get(Attendee, first_item.attendee_id)
    assert attendee is not None
    attendee.email = "named-attendee@example.com"
    second_item.attendee_id = None
    await db.commit()

    for blank in (" ", "   ", "\t", "\n\t "):
        response = await client.get(
            f"{API}/events/{event_id}/tickets/search",
            params={"q": blank},
            headers=bearer(owner_token),
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    trimmed = await client.get(
        f"{API}/events/{event_id}/tickets/search",
        params={"q": f"  {buyer.email}  "},
        headers=bearer(owner_token),
    )
    assert trimmed.status_code == 200, trimmed.text
    assert {row["public_id"] for row in trimmed.json()} == {
        first_ticket["public_id"],
        second_ticket["public_id"],
    }

    fallback_row = next(
        row for row in trimmed.json() if row["public_id"] == second_ticket["public_id"]
    )
    assert fallback_row["attendee_name"] is None
    assert fallback_row["attendee_email"] == buyer.email

    by_attendee = await client.get(
        f"{API}/events/{event_id}/tickets/search",
        params={"q": "named-attendee@example.com"},
        headers=bearer(owner_token),
    )
    assert by_attendee.status_code == 200
    assert [row["public_id"] for row in by_attendee.json()] == [
        first_ticket["public_id"]
    ]

    literal_wildcard = await client.get(
        f"{API}/events/{event_id}/tickets/search",
        params={"q": "%"},
        headers=bearer(owner_token),
    )
    assert literal_wildcard.status_code == 200
    assert literal_wildcard.json() == []


async def test_manual_check_in_uses_server_side_state_and_rejects_duplicates(
    client, app, db
):
    event_id, ticket_type_id, _owner, owner_token, _buyer, buyer_token = (
        await _setup_event(client, app, db)
    )
    order = await _buy(client, buyer_token, event_id, ticket_type_id)
    ticket = order["tickets"][0]

    wrong_event, _wrong_type = await _create_published_event(
        client, owner_token, title="Wrong Event"
    )
    wrong = await client.post(
        f"{API}/events/{wrong_event}/tickets/{ticket['public_id']}/check-in",
        headers=bearer(owner_token),
    )
    assert wrong.status_code == 404
    assert wrong.json()["error"]["code"] == "TICKET_NOT_FOUND"

    forbidden = await client.post(
        f"{API}/events/{event_id}/tickets/{ticket['public_id']}/check-in",
        headers=bearer(buyer_token),
    )
    assert forbidden.status_code == 403

    checked_in = await client.post(
        f"{API}/events/{event_id}/tickets/{ticket['public_id']}/check-in",
        headers=bearer(owner_token),
    )
    assert checked_in.status_code == 200, checked_in.text
    assert checked_in.json()["status"] == "CHECKED_IN"
    assert checked_in.json()["checked_in_at"] is not None

    duplicate = await client.post(
        f"{API}/events/{event_id}/tickets/{ticket['public_id']}/check-in",
        headers=bearer(owner_token),
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "TICKET_ALREADY_CHECKED_IN"

    records = await _checkin_records(db, UUID(ticket["ticket_id"]))
    assert [record.action for record in records] == [CheckinAction.CHECK_IN]
    assert records[0].performed_by == _owner.id
    assert records[0].created_at is not None


async def test_validation_is_read_only(client, app, db):
    event_id, ticket_type_id, _owner, owner_token, _buyer, buyer_token = (
        await _setup_event(client, app, db)
    )
    order = await _buy(client, buyer_token, event_id, ticket_type_id)
    ticket = order["tickets"][0]

    for _ in range(2):
        response = await client.post(
            f"{API}/events/{event_id}/tickets/validate",
            json={"qr_payload": ticket["qr_payload"]},
            headers=bearer(owner_token),
        )
        assert response.status_code == 200
        assert response.json()["valid"] is True

    db.expire_all()
    stored = await db.get(Ticket, UUID(ticket["ticket_id"]))
    assert stored is not None
    assert stored.status == TicketStatus.VALID
    assert stored.checked_in_at is None


async def test_reversal_records_actor_restores_ticket_and_preserves_history(
    client, app, db
):
    event_id, ticket_type_id, owner, owner_token, _buyer, buyer_token = (
        await _setup_event(client, app, db)
    )
    order = await _buy(client, buyer_token, event_id, ticket_type_id)
    ticket = order["tickets"][0]
    ticket_id = UUID(ticket["ticket_id"])

    checked_in = await client.post(
        f"{API}/events/{event_id}/check-in",
        json={"qr_payload": ticket["qr_payload"]},
        headers=bearer(owner_token),
    )
    assert checked_in.status_code == 200

    reversed_response = await client.post(
        f"{API}/events/{event_id}/tickets/{ticket['public_id']}/reverse-check-in",
        headers=bearer(owner_token),
    )
    assert reversed_response.status_code == 200, reversed_response.text
    assert reversed_response.json()["status"] == "VALID"
    assert reversed_response.json()["checked_in_at"] is None

    records = await _checkin_records(db, ticket_id)
    assert [record.action for record in records] == [
        CheckinAction.CHECK_IN,
        CheckinAction.REVERSE,
    ]
    assert records[0].performed_by == owner.id
    assert records[1].performed_by == owner.id
    assert records[1].created_at >= records[0].created_at

    validated = await client.post(
        f"{API}/events/{event_id}/tickets/validate",
        json={"qr_payload": ticket["qr_payload"]},
        headers=bearer(owner_token),
    )
    assert validated.status_code == 200
    assert validated.json()["valid"] is True
    assert validated.json()["reason"] is None

    # The ticket is usable again after the reversal.
    again = await client.post(
        f"{API}/events/{event_id}/check-in",
        json={"qr_payload": ticket["qr_payload"]},
        headers=bearer(owner_token),
    )
    assert again.status_code == 200


async def test_reversal_requires_checked_in_ticket_and_is_not_repeatable(
    client, app, db
):
    event_id, ticket_type_id, _owner, owner_token, _buyer, buyer_token = (
        await _setup_event(client, app, db)
    )
    order = await _buy(client, buyer_token, event_id, ticket_type_id)
    ticket = order["tickets"][0]

    not_checked_in = await client.post(
        f"{API}/events/{event_id}/tickets/{ticket['public_id']}/reverse-check-in",
        headers=bearer(owner_token),
    )
    assert not_checked_in.status_code == 409
    assert not_checked_in.json()["error"]["code"] == "TICKET_NOT_CHECKED_IN"

    await client.post(
        f"{API}/events/{event_id}/tickets/{ticket['public_id']}/check-in",
        headers=bearer(owner_token),
    )
    first = await client.post(
        f"{API}/events/{event_id}/tickets/{ticket['public_id']}/reverse-check-in",
        headers=bearer(owner_token),
    )
    assert first.status_code == 200

    duplicate = await client.post(
        f"{API}/events/{event_id}/tickets/{ticket['public_id']}/reverse-check-in",
        headers=bearer(owner_token),
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "TICKET_NOT_CHECKED_IN"

    records = await _checkin_records(db, UUID(ticket["ticket_id"]))
    assert sum(record.action == CheckinAction.REVERSE for record in records) == 1


async def test_reversal_is_limited_to_owner_platform_admin_and_event_admin(
    client, app, db
):
    event_id, ticket_type_id, _owner, owner_token, _buyer, buyer_token = (
        await _setup_event(client, app, db)
    )
    order = await _buy(client, buyer_token, event_id, ticket_type_id)
    ticket = order["tickets"][0]
    await client.post(
        f"{API}/events/{event_id}/tickets/{ticket['public_id']}/check-in",
        headers=bearer(owner_token),
    )

    staff = await create_user(db, email="checkin-staff@example.com")
    await _assign_staff(
        db, event_id=event_id, user_id=staff.id, role=EventStaffRole.CHECK_IN_STAFF
    )
    staff_token = access_token_for(app, staff)
    staff_checkin = await client.post(
        f"{API}/events/{event_id}/tickets/{ticket['public_id']}/check-in",
        headers=bearer(staff_token),
    )
    assert staff_checkin.status_code == 409  # allowed to try, already used

    staff_reverse = await client.post(
        f"{API}/events/{event_id}/tickets/{ticket['public_id']}/reverse-check-in",
        headers=bearer(staff_token),
    )
    assert staff_reverse.status_code == 403
    assert staff_reverse.json()["error"]["code"] == "INSUFFICIENT_EVENT_PERMISSIONS"

    buyer_reverse = await client.post(
        f"{API}/events/{event_id}/tickets/{ticket['public_id']}/reverse-check-in",
        headers=bearer(buyer_token),
    )
    assert buyer_reverse.status_code == 403

    event_admin = await create_user(db, email="event-admin@example.com")
    await _assign_staff(
        db, event_id=event_id, user_id=event_admin.id, role=EventStaffRole.EVENT_ADMIN
    )
    admin_reverse = await client.post(
        f"{API}/events/{event_id}/tickets/{ticket['public_id']}/reverse-check-in",
        headers=bearer(access_token_for(app, event_admin)),
    )
    assert admin_reverse.status_code == 200


async def test_campaign_qr_is_never_accepted_as_admission(client, app, db):
    event_id, _ticket_type_id, _owner, owner_token, _buyer, _buyer_token = (
        await _setup_event(client, app, db)
    )
    token = uuid4()
    db.add(
        Campaign(
            event_id=UUID(event_id),
            name="Promo QR",
            campaign_token=token,
            discount_type=DiscountType.PERCENT,
            discount_value=Decimal("10"),
            status=CampaignStatus.ACTIVE,
        )
    )
    await db.commit()

    for path in ("tickets/validate", "check-in"):
        response = await client.post(
            f"{API}/events/{event_id}/{path}",
            json={"qr_payload": str(token)},
            headers=bearer(owner_token),
        )
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "CAMPAIGN_QR_NOT_ADMISSION"


async def test_refunded_and_cancelled_tickets_cannot_be_admitted(client, app, db):
    event_id, ticket_type_id, _owner, owner_token, _buyer, buyer_token = (
        await _setup_event(client, app, db)
    )
    order = await _buy(client, buyer_token, event_id, ticket_type_id, quantity=2)
    refunded = order["tickets"][0]
    cancelled = order["tickets"][1]

    refunded_row = await db.get(Ticket, UUID(refunded["ticket_id"]))
    cancelled_row = await db.get(Ticket, UUID(cancelled["ticket_id"]))
    assert refunded_row is not None and cancelled_row is not None
    refunded_row.status = TicketStatus.REFUNDED
    cancelled_row.status = TicketStatus.CANCELLED
    cancelled_row.cancelled_at = _now()
    await db.commit()

    for ticket, reason in (
        (refunded, "TICKET_REFUNDED"),
        (cancelled, "TICKET_CANCELLED"),
    ):
        validated = await client.post(
            f"{API}/events/{event_id}/tickets/validate",
            json={"qr_payload": ticket["qr_payload"]},
            headers=bearer(owner_token),
        )
        assert validated.status_code == 200
        assert validated.json()["valid"] is False
        assert validated.json()["reason"] == reason

        denied = await client.post(
            f"{API}/events/{event_id}/check-in",
            json={"qr_payload": ticket["qr_payload"]},
            headers=bearer(owner_token),
        )
        assert denied.status_code == 409
        assert denied.json()["error"]["code"] == "TICKET_NOT_VALID"


async def test_attendance_stats_before_and_after_reversal(client, app, db):
    event_id, ticket_type_id, _owner, owner_token, _buyer, buyer_token = (
        await _setup_event(client, app, db)
    )
    order = await _buy(client, buyer_token, event_id, ticket_type_id, quantity=3)
    refunded, cancelled, usable = order["tickets"]

    refunded_row = await db.get(Ticket, UUID(refunded["ticket_id"]))
    cancelled_row = await db.get(Ticket, UUID(cancelled["ticket_id"]))
    assert refunded_row is not None and cancelled_row is not None
    refunded_row.status = TicketStatus.REFUNDED
    cancelled_row.status = TicketStatus.CANCELLED
    cancelled_row.cancelled_at = _now()
    await db.commit()

    stats = await client.get(
        f"{API}/events/{event_id}/tickets/stats", headers=bearer(owner_token)
    )
    assert stats.status_code == 200, stats.text
    body = stats.json()
    assert body == {
        "event_id": event_id,
        "total_registered": 1,
        "total_checked_in": 0,
        "remaining": 1,
        "cancelled": 1,
        "refunded": 1,
        "total_issued": 3,
    }

    await client.post(
        f"{API}/events/{event_id}/tickets/{usable['public_id']}/check-in",
        headers=bearer(owner_token),
    )
    after_checkin = (
        await client.get(
            f"{API}/events/{event_id}/tickets/stats", headers=bearer(owner_token)
        )
    ).json()
    assert after_checkin["total_registered"] == 1
    assert after_checkin["total_checked_in"] == 1
    assert after_checkin["remaining"] == 0

    await client.post(
        f"{API}/events/{event_id}/tickets/{usable['public_id']}/reverse-check-in",
        headers=bearer(owner_token),
    )
    after_reversal = (
        await client.get(
            f"{API}/events/{event_id}/tickets/stats", headers=bearer(owner_token)
        )
    ).json()
    assert after_reversal["total_registered"] == 1
    assert after_reversal["total_checked_in"] == 0
    assert after_reversal["remaining"] == 1

    forbidden = await client.get(
        f"{API}/events/{event_id}/tickets/stats", headers=bearer(buyer_token)
    )
    assert forbidden.status_code == 403


async def test_wrong_event_and_malformed_qr_are_rejected(client, app, db):
    event_a, type_a, _owner, owner_token, _buyer, buyer_token = await _setup_event(
        client, app, db
    )
    event_b, type_b = await _create_published_event(
        client, owner_token, title="Other Event"
    )
    order_a = await _buy(client, buyer_token, event_a, type_a)
    order_b = await _buy(client, buyer_token, event_b, type_b)

    wrong_event = await client.post(
        f"{API}/events/{event_a}/tickets/validate",
        json={"qr_payload": order_b["tickets"][0]["qr_payload"]},
        headers=bearer(owner_token),
    )
    assert wrong_event.status_code == 404
    assert wrong_event.json()["error"]["code"] == "TICKET_NOT_FOUND"

    malformed_payloads = (
        "",
        "not-a-ticket",
        "v1.not-a-uuid.signature",
        order_a["tickets"][0]["qr_payload"] + "x",
    )
    for malformed in malformed_payloads:
        response = await client.post(
            f"{API}/events/{event_a}/tickets/validate",
            json={"qr_payload": malformed},
            headers=bearer(owner_token),
        )
        assert response.status_code in {401, 422}
        if response.status_code == 401:
            assert response.json()["error"]["code"] == "INVALID_TICKET_QR"


@needs_pg
async def test_concurrent_check_in_creates_one_record(client, app, db):
    event_id, ticket_type_id, _owner, owner_token, _buyer, buyer_token = (
        await _setup_event(client, app, db)
    )
    order = await _buy(client, buyer_token, event_id, ticket_type_id)
    ticket = order["tickets"][0]

    responses = await asyncio.gather(
        *[
            client.post(
                f"{API}/events/{event_id}/check-in",
                json={"qr_payload": ticket["qr_payload"]},
                headers=bearer(owner_token),
            )
            for _ in range(2)
        ]
    )
    assert sorted(response.status_code for response in responses) == [200, 409]

    records = await _checkin_records(db, UUID(ticket["ticket_id"]))
    assert sum(record.action == CheckinAction.CHECK_IN for record in records) == 1


@needs_pg
async def test_concurrent_reversals_create_one_record(client, app, db):
    event_id, ticket_type_id, _owner, owner_token, _buyer, buyer_token = (
        await _setup_event(client, app, db)
    )
    order = await _buy(client, buyer_token, event_id, ticket_type_id)
    ticket = order["tickets"][0]
    await client.post(
        f"{API}/events/{event_id}/tickets/{ticket['public_id']}/check-in",
        headers=bearer(owner_token),
    )

    responses = await asyncio.gather(
        *[
            client.post(
                f"{API}/events/{event_id}/tickets/{ticket['public_id']}/reverse-check-in",
                headers=bearer(owner_token),
            )
            for _ in range(2)
        ]
    )
    assert sorted(response.status_code for response in responses) == [200, 409]

    records = await _checkin_records(db, UUID(ticket["ticket_id"]))
    assert sum(record.action == CheckinAction.REVERSE for record in records) == 1


@needs_pg
async def test_concurrent_check_in_and_reversal_serialize(client, app, db):
    event_id, ticket_type_id, _owner, owner_token, _buyer, buyer_token = (
        await _setup_event(client, app, db)
    )
    order = await _buy(client, buyer_token, event_id, ticket_type_id)
    ticket = order["tickets"][0]
    ticket_id = UUID(ticket["ticket_id"])
    await client.post(
        f"{API}/events/{event_id}/tickets/{ticket['public_id']}/check-in",
        headers=bearer(owner_token),
    )

    checkin_response, reversal_response = await asyncio.gather(
        client.post(
            f"{API}/events/{event_id}/tickets/{ticket['public_id']}/check-in",
            headers=bearer(owner_token),
        ),
        client.post(
            f"{API}/events/{event_id}/tickets/{ticket['public_id']}/reverse-check-in",
            headers=bearer(owner_token),
        ),
    )
    assert reversal_response.status_code == 200
    assert checkin_response.status_code in {200, 409}

    db.expire_all()
    stored = await db.get(Ticket, ticket_id)
    assert stored is not None
    records = await _checkin_records(db, ticket_id)
    checkins = sum(record.action == CheckinAction.CHECK_IN for record in records)
    reversals = sum(record.action == CheckinAction.REVERSE for record in records)
    assert reversals == 1
    if stored.status == TicketStatus.CHECKED_IN:
        assert checkins == 2
    else:
        assert stored.status == TicketStatus.VALID
        assert checkins == 1
