"""Reported events, platform settings, event image uploads, calendar export
and health checks (SRS 4.12, 4.2, 8)."""

import io
from decimal import Decimal
from uuid import UUID

import pytest
from PIL import Image
from sqlalchemy import select

from app.db.models import AuditLog, EventSalesActivation, Notification, UserRole
from tests.conftest import API, access_token_for, bearer, create_user
from tests.test_orders import _make_event
from tests.test_paid_sales import _create_owner


async def _admin(app, db, email="settings-admin@example.com"):
    user = await create_user(db, email=email, role=UserRole.PLATFORM_ADMIN,
                             first_name="Root", last_name="Admin")
    return user, access_token_for(app, user)


async def _published(client, app, db, prefix="rep"):
    owner, owner_token = await _create_owner(client, app, db, f"{prefix}-owner@example.com")
    event, ticket_type = await _make_event(client, owner_token)
    return owner, owner_token, event, ticket_type


async def _user(app, db, email):
    user = await create_user(db, email=email)
    return user, access_token_for(app, user)


def _png(width=40, height=20, fmt="PNG") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buffer, format=fmt)
    return buffer.getvalue()


# ---------- Reported events ----------


async def test_user_reports_event_and_admin_resolves_it(client, app, db):
    _owner, _owner_token, event, _tt = await _published(client, app, db)
    reporter, token = await _user(app, db, "reporter@example.com")
    reporter_id = reporter.id
    _admin_user, admin_token = await _admin(app, db)

    created = await client.post(
        f"{API}/events/{event['id']}/reports",
        json={"reason": "FRAUD", "details": "  Fake venue  "},
        headers=bearer(token),
    )
    assert created.status_code == 201, created.text
    report = created.json()
    assert report["status"] == "OPEN"
    assert report["details"] == "Fake venue"

    # One active report per user and event.
    again = await client.post(
        f"{API}/events/{event['id']}/reports", json={"reason": "SPAM"}, headers=bearer(token)
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "REPORT_ALREADY_OPEN"

    mine = await client.get(f"{API}/users/me/reports", headers=bearer(token))
    assert mine.status_code == 200
    assert [r["id"] for r in mine.json()["items"]] == [report["id"]]

    listed = await client.get(
        f"{API}/admin/reports", params={"status": "OPEN"}, headers=bearer(admin_token)
    )
    assert listed.status_code == 200, listed.text
    item = listed.json()["items"][0]
    assert item["event"]["id"] == event["id"]
    assert item["reporter_email"] == "reporter@example.com"
    assert item["event_active_reports"] == 1
    assert item["event_is_suspended"] is False

    review = await client.patch(
        f"{API}/admin/reports/{report['id']}",
        json={"status": "UNDER_REVIEW"},
        headers=bearer(admin_token),
    )
    assert review.status_code == 200, review.text
    assert review.json()["status"] == "UNDER_REVIEW"

    resolved = await client.patch(
        f"{API}/admin/reports/{report['id']}",
        json={"status": "RESOLVED", "resolution_note": "Confirmed fraud",
              "suspend_event": True},
        headers=bearer(admin_token),
    )
    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    assert body["status"] == "RESOLVED"
    assert body["event_is_suspended"] is True
    assert body["event_active_reports"] == 0
    assert body["resolved_at"] is not None

    # Terminal: no further transitions.
    reopen = await client.patch(
        f"{API}/admin/reports/{report['id']}", json={"status": "OPEN"},
        headers=bearer(admin_token),
    )
    assert reopen.status_code == 409

    # Suspended events are hidden, so they can no longer be reported.
    _other, other_token = await _user(app, db, "reporter2@example.com")
    hidden = await client.post(
        f"{API}/events/{event['id']}/reports", json={"reason": "SPAM"},
        headers=bearer(other_token),
    )
    assert hidden.status_code == 404

    db.expire_all()
    note = (await db.execute(
        select(Notification).where(
            Notification.user_id == reporter_id, Notification.type == "EVENT_REPORT_REVIEWED"
        )
    )).scalar_one()
    assert note.payload["status"] == "RESOLVED"
    actions = set((await db.execute(
        select(AuditLog.action).where(AuditLog.event_id == UUID(event["id"]))
    )).scalars())
    assert {"event.reported", "admin.event_report_updated", "admin.event_suspended"} <= actions


async def test_report_validation_and_permissions(client, app, db):
    _owner, owner_token, event, _tt = await _published(client, app, db, "repv")
    _user_row, token = await _user(app, db, "repv-user@example.com")

    own = await client.post(
        f"{API}/events/{event['id']}/reports", json={"reason": "SPAM"},
        headers=bearer(owner_token),
    )
    assert own.status_code == 409
    assert own.json()["error"]["code"] == "CANNOT_REPORT_OWN_EVENT"

    other = await client.post(
        f"{API}/events/{event['id']}/reports", json={"reason": "OTHER"}, headers=bearer(token)
    )
    assert other.status_code == 422  # OTHER needs details

    anonymous = await client.post(
        f"{API}/events/{event['id']}/reports", json={"reason": "SPAM"}
    )
    assert anonymous.status_code == 401

    unverified = await create_user(db, email="repv-unverified@example.com", verified=False)
    response = await client.post(
        f"{API}/events/{event['id']}/reports", json={"reason": "SPAM"},
        headers=bearer(access_token_for(app, unverified)),
    )
    assert response.status_code == 403

    for path in ("/admin/reports", "/admin/settings"):
        denied = await client.get(f"{API}{path}", headers=bearer(token))
        assert denied.status_code == 403

    _admin_user, admin_token = await _admin(app, db, "repv-admin@example.com")
    bad = await client.patch(
        f"{API}/admin/reports/00000000-0000-0000-0000-000000000001",
        json={"status": "DISMISSED", "suspend_event": True},
        headers=bearer(admin_token),
    )
    assert bad.status_code == 422  # suspend only with RESOLVED
    missing = await client.patch(
        f"{API}/admin/reports/00000000-0000-0000-0000-000000000001",
        json={"status": "DISMISSED"},
        headers=bearer(admin_token),
    )
    assert missing.status_code == 404


async def test_dismissed_report_allows_new_report(client, app, db):
    _owner, _ot, event, _tt = await _published(client, app, db, "repd")
    _u, token = await _user(app, db, "repd-user@example.com")
    _a, admin_token = await _admin(app, db, "repd-admin@example.com")
    first = await client.post(
        f"{API}/events/{event['id']}/reports", json={"reason": "SPAM"}, headers=bearer(token)
    )
    dismissed = await client.patch(
        f"{API}/admin/reports/{first.json()['id']}", json={"status": "DISMISSED"},
        headers=bearer(admin_token),
    )
    assert dismissed.status_code == 200
    assert dismissed.json()["event_is_suspended"] is False
    second = await client.post(
        f"{API}/events/{event['id']}/reports", json={"reason": "MISLEADING"},
        headers=bearer(token),
    )
    assert second.status_code == 201, second.text


# ---------- Platform settings ----------


async def test_platform_settings_defaults_update_and_audit(client, app, db):
    _a, admin_token = await _admin(app, db)
    current = await client.get(f"{API}/admin/settings", headers=bearer(admin_token))
    assert current.status_code == 200, current.text
    assert Decimal(current.json()["activation_fee_amount"]) == Decimal("1000.00")
    assert Decimal(current.json()["processing_fee_percent"]) == Decimal("3.00")

    empty = await client.patch(f"{API}/admin/settings", json={}, headers=bearer(admin_token))
    assert empty.status_code == 422
    too_high = await client.patch(
        f"{API}/admin/settings", json={"processing_fee_percent": "25"},
        headers=bearer(admin_token),
    )
    assert too_high.status_code == 422

    updated = await client.patch(
        f"{API}/admin/settings",
        json={"activation_fee_amount": "2500", "processing_fee_percent": "5",
              "reason": "New pricing"},
        headers=bearer(admin_token),
    )
    assert updated.status_code == 200, updated.text
    assert Decimal(updated.json()["activation_fee_amount"]) == Decimal("2500.00")
    assert Decimal(updated.json()["processing_fee_percent"]) == Decimal("5.00")

    actions = await client.get(
        f"{API}/admin/actions", params={"action": "admin.platform_settings_updated"},
        headers=bearer(admin_token),
    )
    entry = actions.json()["items"][0]
    assert entry["reason"] == "New pricing"
    assert entry["before"]["processing_fee_percent"] == "3.00"
    assert entry["after"]["processing_fee_percent"] == "5.00"


async def test_settings_apply_to_new_orders_and_unpaid_activation_fees(client, app, db):
    owner, owner_token, event, ticket_type = await _published(client, app, db, "fee")
    _a, admin_token = await _admin(app, db, "fee-admin@example.com")

    status = await client.get(
        f"{API}/events/{event['id']}/paid-sales/activation", headers=bearer(owner_token)
    )
    assert Decimal(status.json()["activation_fee_amount"]) == Decimal("1000.00")

    await client.patch(
        f"{API}/admin/settings",
        json={"activation_fee_amount": "1500", "processing_fee_percent": "10"},
        headers=bearer(admin_token),
    )
    status = await client.get(
        f"{API}/events/{event['id']}/paid-sales/activation", headers=bearer(owner_token)
    )
    assert Decimal(status.json()["activation_fee_amount"]) == Decimal("1500.00")

    paid = await client.post(
        f"{API}/events/{event['id']}/paid-sales/activation-fee", headers=bearer(owner_token)
    )
    assert paid.status_code in (200, 201), paid.text
    # A paid fee is a fixed record.
    from tests.test_orders import _activate_paid_sales

    await _activate_paid_sales(client, db, owner, owner_token, event["id"])
    await client.patch(
        f"{API}/admin/settings", json={"activation_fee_amount": "99"},
        headers=bearer(admin_token),
    )
    db.expire_all()
    activation = await db.get(EventSalesActivation, UUID(event["id"]))
    assert activation is not None
    assert activation.activation_fee_amount == Decimal("1500.00")

    # New orders use the 10% fee (ticket price 25.00 -> fee 2.50).
    _b, buyer_token = await _user(app, db, "fee-buyer@example.com")
    order = await client.post(
        f"{API}/orders",
        json={"event_id": event["id"],
              "items": [{"ticket_type_id": ticket_type["id"], "quantity": 1}]},
        headers=bearer(buyer_token),
    )
    assert order.status_code == 201, order.text
    assert Decimal(order.json()["fee_amount"]) == Decimal("2.50")
    assert Decimal(order.json()["total_amount"]) == Decimal("27.50")


# ---------- Event image upload and media ----------


@pytest.fixture
def media_root(app, tmp_path):
    app.state.settings = app.state.settings.model_copy(update={"media_root": str(tmp_path)})
    return tmp_path


async def test_owner_uploads_image_and_it_is_served(client, app, db, media_root):
    _owner, owner_token, event, _tt = await _published(client, app, db, "img")

    uploaded = await client.post(
        f"{API}/events/{event['id']}/images/upload",
        files={"file": ("cover.png", _png(), "image/png")},
        headers=bearer(owner_token),
    )
    assert uploaded.status_code == 201, uploaded.text
    body = uploaded.json()
    assert body["content_type"] == "image/png"
    assert (body["width"], body["height"]) == (40, 20)
    assert body["object_key"] == body["url"]
    assert body["url"].startswith(f"{API}/media/events/{event['id']}/")

    served = await client.get(body["url"])
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(served.content)).size == (40, 20)

    # Attach via the existing event update contract.
    patched = await client.patch(
        f"{API}/events/{event['id']}",
        json={"images": [{"object_key": body["object_key"], "alt_text": "Cover"}]},
        headers=bearer(owner_token),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["images"][0]["object_key"] == body["object_key"]


async def test_upload_rejects_bad_files_and_non_owners(client, app, db, media_root):
    _owner, owner_token, event, _tt = await _published(client, app, db, "imgbad")
    _u, other_token = await _user(app, db, "imgbad-other@example.com")
    url = f"{API}/events/{event['id']}/images/upload"

    denied = await client.post(
        url, files={"file": ("a.png", _png(), "image/png")}, headers=bearer(other_token)
    )
    assert denied.status_code == 403

    fake = await client.post(
        url, files={"file": ("a.png", b"<svg onload=alert(1)>", "image/png")},
        headers=bearer(owner_token),
    )
    assert fake.status_code == 415
    gif = await client.post(
        url, files={"file": ("a.gif", _png(fmt="GIF"), "image/gif")},
        headers=bearer(owner_token),
    )
    assert gif.status_code == 415

    app.state.settings = app.state.settings.model_copy(update={"media_max_upload_bytes": 1024})
    big = await client.post(
        url, files={"file": ("a.jpg", _png(800, 800, "JPEG"), "image/jpeg")},
        headers=bearer(owner_token),
    )
    assert big.status_code == 413
    assert list(media_root.rglob("*.*")) == []


async def test_media_route_only_serves_valid_keys(client, media_root):
    for key in ("../pyproject.toml", "events/x/y.png", "events/%2e%2e/secret.png"):
        response = await client.get(f"{API}/media/{key}")
        assert response.status_code == 404


# ---------- Calendar export ----------


async def test_calendar_export(client, app, db):
    _owner, owner_token, event, _tt = await _published(client, app, db, "ics")
    response = await client.get(f"{API}/events/{event['id']}/calendar.ics")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/calendar")
    text = response.text
    assert text.startswith("BEGIN:VCALENDAR\r\n")
    assert f"UID:{event['id']}@biletflow\r\n" in text
    assert "SUMMARY:Order Test Event\r\n" in text
    assert "STATUS:CONFIRMED" in text
    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))

    draft = await client.post(
        f"{API}/events",
        json={"title": "Draft", "timezone": "UTC",
              "starts_at": event["starts_at"], "ends_at": event["ends_at"]},
        headers=bearer(owner_token),
    )
    hidden = await client.get(f"{API}/events/{draft.json()['id']}/calendar.ics")
    assert hidden.status_code == 404


def test_ics_escaping_and_folding():
    from datetime import datetime, timezone

    from app.db.models import Event, EventStatus
    from app.services.calendar import build_event_ics

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    event = Event(
        id=UUID("11111111-1111-1111-1111-111111111111"), title="Rock; Roll, Live\\",
        description="Line one\nЖыл " * 10, venue_name="Hall", city="Almaty",
        timezone="Asia/Almaty", starts_at=now, ends_at=now, updated_at=now,
        status=EventStatus.CANCELLED,
    )
    text = build_event_ics(event, url="https://example.com/catalog/1", now=now)
    assert "SUMMARY:Rock\\; Roll\\, Live\\\\\r\n" in text
    assert "STATUS:CANCELLED" in text
    assert "LOCATION:Hall\\, Almaty" in text
    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))
    unfolded = text.replace("\r\n ", "")
    assert "Line one\\nЖыл" in unfolded


# ---------- Health ----------


async def test_health_endpoints(client):
    live = await client.get("/health/live")
    assert live.status_code == 200
    ready = await client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ok", "checks": {"database": "ok"}}
