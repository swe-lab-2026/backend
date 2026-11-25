"""Report confidentiality in organizer feeds, demo shortcuts vs admin decisions,
the admin payout-account queue and payout status notifications (SRS 4.10)."""

from sqlalchemy import select

from app.db.models import Notification, PayoutAccount, UserRole
from tests.conftest import API, access_token_for, bearer, create_user
from tests.test_orders import _make_event
from tests.test_paid_sales import _create_owner

PAYOUT = {"provider": "DEMO", "external_account_ref": "demo-account-0042", "is_default": True}


async def _admin(app, db, email="review-admin@example.com"):
    user = await create_user(db, email=email, role=UserRole.PLATFORM_ADMIN)
    return user, access_token_for(app, user)


async def _organizer(client, app, db, email="payout-owner@example.com"):
    user = await create_user(db, email=email)
    token = access_token_for(app, user)
    resp = await client.post(
        f"{API}/organizers/me",
        json={"display_name": "Payout Org", "terms_accepted": True},
        headers=bearer(token),
    )
    assert resp.status_code == 201
    return user, token


async def _notifications(db, user_id, kind):
    return (await db.execute(
        select(Notification).where(Notification.user_id == user_id, Notification.type == kind)
    )).scalars().all()


# ---------- Reporter identity never reaches the organizer ----------


async def test_owner_feeds_hide_event_reports(client, app, db):
    _owner, owner_token = await _create_owner(client, app, db, "priv-owner@example.com")
    event, _tt = await _make_event(client, owner_token)
    reporter = await create_user(
        db, email="secret-reporter@example.com", first_name="Secret", last_name="Reporter"
    )
    _admin_user, admin_token = await _admin(app, db)

    created = await client.post(
        f"{API}/events/{event['id']}/reports", json={"reason": "FRAUD"},
        headers=bearer(access_token_for(app, reporter)),
    )
    assert created.status_code == 201
    resolved = await client.patch(
        f"{API}/admin/reports/{created.json()['id']}",
        json={"status": "DISMISSED", "resolution_note": "Checked"},
        headers=bearer(admin_token),
    )
    assert resolved.status_code == 200, resolved.text

    timeline = await client.get(
        f"{API}/events/{event['id']}/timeline", params={"limit": 200},
        headers=bearer(owner_token),
    )
    assert timeline.status_code == 200
    audit = await client.get(
        f"{API}/events/{event['id']}/audit-log", params={"limit": 200},
        headers=bearer(owner_token),
    )
    assert audit.status_code == 200
    for body in (timeline.text, audit.text):
        assert "Secret" not in body
        assert str(reporter.id) not in body
        assert "event_report" not in body
    # The feeds still work for everything else.
    assert timeline.json()["items"]
    assert audit.json()


# ---------- Demo shortcuts cannot overturn an admin ----------


async def test_simulated_verification_cannot_undo_rejection(client, app, db):
    user, token = await _organizer(client, app, db)
    _admin_user, admin_token = await _admin(app, db)
    rejected = await client.post(
        f"{API}/admin/organizers/{user.id}/verification",
        json={"status": "REJECTED", "reason": "Documents invalid"},
        headers=bearer(admin_token),
    )
    assert rejected.status_code == 200, rejected.text

    resp = await client.post(f"{API}/organizers/me/verification/simulate", headers=bearer(token))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "VERIFICATION_REJECTED"
    profile = await client.get(f"{API}/organizers/me", headers=bearer(token))
    assert profile.json()["verification_status"] == "REJECTED"


async def test_simulated_payout_approval_cannot_undo_block(client, app, db):
    user, token = await _organizer(client, app, db)
    _admin_user, admin_token = await _admin(app, db)
    account = (await client.put(
        f"{API}/organizers/me/payout-account", json=PAYOUT, headers=bearer(token)
    )).json()
    blocked = await client.post(
        f"{API}/admin/payout-accounts/{account['id']}/status",
        json={"status": "BLOCKED", "reason": "Fraud check"},
        headers=bearer(admin_token),
    )
    assert blocked.status_code == 200, blocked.text

    resp = await client.post(
        f"{API}/organizers/me/payout-account/simulate-approval", headers=bearer(token)
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "PAYOUT_ACCOUNT_BLOCKED"

    # A fresh account does not get around the block either.
    fresh = await client.put(
        f"{API}/organizers/me/payout-account",
        json={**PAYOUT, "external_account_ref": "demo-account-9999"},
        headers=bearer(token),
    )
    assert fresh.status_code == 200
    resp = await client.post(
        f"{API}/organizers/me/payout-account/simulate-approval", headers=bearer(token)
    )
    assert resp.status_code == 409
    statuses = (await db.execute(
        select(PayoutAccount.status).where(PayoutAccount.organizer_user_id == user.id)
    )).scalars().all()
    assert "ACTIVE" not in {s.value for s in statuses}


async def test_simulation_still_works_without_admin_decision(client, app, db):
    _user, token = await _organizer(client, app, db)
    await client.put(f"{API}/organizers/me/payout-account", json=PAYOUT, headers=bearer(token))
    verified = await client.post(
        f"{API}/organizers/me/verification/simulate", headers=bearer(token)
    )
    assert verified.status_code == 200
    assert verified.json()["verification_status"] == "VERIFIED"
    approved = await client.post(
        f"{API}/organizers/me/payout-account/simulate-approval", headers=bearer(token)
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "ACTIVE"


# ---------- Admin payout-account queue ----------


async def test_admin_lists_payout_accounts(client, app, db):
    user, token = await _organizer(client, app, db)
    other, other_token = await _organizer(client, app, db, email="second-org@example.com")
    _admin_user, admin_token = await _admin(app, db)
    mine = (await client.put(
        f"{API}/organizers/me/payout-account", json=PAYOUT, headers=bearer(token)
    )).json()
    await client.put(
        f"{API}/organizers/me/payout-account",
        json={**PAYOUT, "external_account_ref": "demo-account-7777"},
        headers=bearer(other_token),
    )
    await client.post(
        f"{API}/admin/payout-accounts/{mine['id']}/status",
        json={"status": "ACTIVE", "reason": "Bank checked"},
        headers=bearer(admin_token),
    )

    resp = await client.get(f"{API}/admin/payout-accounts", headers=bearer(admin_token))
    assert resp.status_code == 200
    page = resp.json()
    assert page["total"] == 2
    row = next(i for i in page["items"] if i["id"] == mine["id"])
    assert row["organizer"] == {"id": str(user.id), "name": "Payout Org"}
    assert row["organizer_email"] == "payout-owner@example.com"
    assert row["status"] == "ACTIVE"
    assert row["external_account_ref"] == "****0042"

    pending = (await client.get(
        f"{API}/admin/payout-accounts", params={"status": "PENDING"},
        headers=bearer(admin_token),
    )).json()
    assert [i["organizer"]["id"] for i in pending["items"]] == [str(other.id)]
    searched = (await client.get(
        f"{API}/admin/payout-accounts", params={"q": "second-org"},
        headers=bearer(admin_token),
    )).json()
    assert searched["total"] == 1

    forbidden = await client.get(f"{API}/admin/payout-accounts", headers=bearer(token))
    assert forbidden.status_code == 403


# ---------- Payout status notifications (SRS 4.10) ----------


async def test_organizer_notified_of_payout_status_change(client, app, db):
    user, token = await _organizer(client, app, db)
    _admin_user, admin_token = await _admin(app, db)
    account = (await client.put(
        f"{API}/organizers/me/payout-account", json=PAYOUT, headers=bearer(token)
    )).json()
    for status in ("ACTIVE", "ACTIVE", "BLOCKED"):
        resp = await client.post(
            f"{API}/admin/payout-accounts/{account['id']}/status",
            json={"status": status, "reason": "Review"},
            headers=bearer(admin_token),
        )
        assert resp.status_code == 200, resp.text

    sent = await _notifications(db, user.id, "PAYOUT_STATUS_CHANGED")
    # The repeated ACTIVE is not a change and sends nothing.
    assert sorted((n.payload["previous_status"], n.payload["status"]) for n in sent) == [
        ("ACTIVE", "BLOCKED"), ("PENDING", "ACTIVE"),
    ]
    assert {n.payload["reason"] for n in sent} == {"Review"}


async def test_self_simulation_sends_no_notification(client, app, db):
    user, token = await _organizer(client, app, db)
    await client.put(f"{API}/organizers/me/payout-account", json=PAYOUT, headers=bearer(token))
    await client.post(
        f"{API}/organizers/me/payout-account/simulate-approval", headers=bearer(token)
    )
    assert await _notifications(db, user.id, "PAYOUT_STATUS_CHANGED") == []
