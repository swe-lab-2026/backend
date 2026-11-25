"""Frontend request (SCRUM-21/23/24/25): organizer verification and payout
approval, availability, attendee details, audit-log filters, the support and
admin contracts, CSV export and the automatic COMPLETED transition."""

import csv
import io
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select, update

from app.db.models import (
    AuditLog,
    Event,
    EventStaff,
    EventStaffRole,
    EventStatus,
    UserRole,
)
from app.services import events as event_service
from tests.conftest import API, access_token_for, bearer, create_user
from tests.test_campaigns import _buyer, _campaign, _item, _order, _pay, _setup
from tests.test_orders import _make_event
from tests.test_paid_sales import _create_owner


async def _admin(app, db, email="contract-admin@example.com"):
    user = await create_user(db, email=email, role=UserRole.PLATFORM_ADMIN,
                             first_name="Ada", last_name="Admin")
    return user, access_token_for(app, user)


# ---------- 2. Organizer verification / payout approval ----------

async def test_paid_sales_can_be_activated_without_database_edits(client, app, db):
    _owner, token = await _create_owner(client, app, db, "selfserve@example.com")
    event, _tt = await _make_event(client, token)
    assert (await client.get(f"{API}/organizers/me/payout-account",
                             headers=bearer(token))).status_code == 404
    put = await client.put(f"{API}/organizers/me/payout-account",
                           json={"provider": "DEMO_BANK", "external_account_ref": "KZ123456789",
                                 "is_default": True}, headers=bearer(token))
    assert put.status_code == 200 and put.json()["status"] == "PENDING"
    got = await client.get(f"{API}/organizers/me/payout-account", headers=bearer(token))
    assert got.status_code == 200 and got.json()["external_account_ref"] == "****6789"

    verified = await client.post(f"{API}/organizers/me/verification/simulate",
                                 headers=bearer(token))
    assert verified.status_code == 200 and verified.json()["verification_status"] == "VERIFIED"
    approved = await client.post(f"{API}/organizers/me/payout-account/simulate-approval",
                                 headers=bearer(token))
    assert approved.status_code == 200 and approved.json()["status"] == "ACTIVE"

    for step in ("terms", "activation-fee", "activate"):
        body = {"accepted": True} if step == "terms" else None
        response = await client.post(f"{API}/events/{event['id']}/paid-sales/{step}",
                                     json=body, headers=bearer(token))
        assert response.status_code == 200, (step, response.text)
    assert response.json()["is_activated"] is True
    detail = await client.get(f"{API}/events/{event['id']}")
    assert detail.json()["paid_sales_active"] is True

    actions = set((await db.execute(select(AuditLog.action))).scalars().all())
    assert {"organizer.verification_simulated", "organizer.payout_approval_simulated"} <= actions


async def test_demo_simulation_can_be_disabled_and_admin_approves(client, app, db):
    owner, token = await _create_owner(client, app, db, "noselfserve@example.com")
    await client.put(f"{API}/organizers/me/payout-account",
                     json={"provider": "DEMO_BANK", "external_account_ref": "ACC-1",
                           "is_default": True}, headers=bearer(token))
    app.state.settings.demo_simulations_enabled = False
    try:
        for path in ("/organizers/me/verification/simulate",
                     "/organizers/me/payout-account/simulate-approval"):
            denied = await client.post(f"{API}{path}", headers=bearer(token))
            assert denied.status_code == 403
            assert denied.json()["error"]["code"] == "DEMO_SIMULATION_DISABLED"
    finally:
        app.state.settings.demo_simulations_enabled = True

    _admin_user, admin_token = await _admin(app, db)
    assert (await client.post(f"{API}/admin/organizers/{owner.id}/verification",
                              json={"status": "VERIFIED"},
                              headers=bearer(token))).status_code == 403
    verified = await client.post(f"{API}/admin/organizers/{owner.id}/verification",
                                 json={"status": "VERIFIED", "reason": "Documents checked"},
                                 headers=bearer(admin_token))
    assert verified.status_code == 200 and verified.json()["verification_status"] == "VERIFIED"
    account = (await client.get(f"{API}/organizers/me/payout-account",
                                headers=bearer(token))).json()
    active = await client.post(f"{API}/admin/payout-accounts/{account['id']}/status",
                               json={"status": "ACTIVE"}, headers=bearer(admin_token))
    assert active.status_code == 200 and active.json()["status"] == "ACTIVE"
    actions = (await client.get(f"{API}/admin/actions", headers=bearer(admin_token))).json()
    assert {a["action"] for a in actions["items"]} == {
        "admin.organizer_verification_set", "admin.payout_account_status_set"}


# ---------- 3. Availability, paid_sales_active, attendees ----------

async def test_ticket_type_availability_and_paid_sales_flag(client, app, db):
    _owner, owner_token, event, ga, vip = await _setup(client, app, db, prefix="avail")
    _admin_user, admin_token = await _admin(app, db)
    _b, token = await _buyer(app, db, "avail-buyer@example.com")
    paid = (await _order(client, token, event["id"], [_item(ga, 2)])).json()
    await _pay(client, token, paid["id"])
    await _order(client, token, event["id"], [_item(ga)])  # pending: active hold

    guest = await client.get(f"{API}/events/{event['id']}/ticket-types")
    assert guest.status_code == 200
    available = {t["name"]: t["available"] for t in guest.json()}
    assert available == {"General Admission": 7, "VIP": 10}

    # Event capacity caps availability across ticket types.
    await client.patch(f"{API}/events/{event['id']}", json={"capacity": 5},
                       headers=bearer(owner_token))
    capped = {t["name"]: t["available"] for t in (await client.get(
        f"{API}/events/{event['id']}/ticket-types")).json()}
    assert capped == {"General Admission": 2, "VIP": 2}

    assert (await client.get(f"{API}/events/{event['id']}")).json()["paid_sales_active"] is True
    await client.post(f"{API}/admin/events/{event['id']}/paid-sales/stop",
                      json={"reason": "review"}, headers=bearer(admin_token))
    assert (await client.get(f"{API}/events/{event['id']}")).json()["paid_sales_active"] is False
    del vip


async def test_order_accepts_one_attendee_per_ticket(client, app, db):
    _owner, _owner_token, event, ga, vip = await _setup(client, app, db, prefix="att")
    buyer, token = await _buyer(app, db, "att-buyer@example.com")
    attendees = [
        {"ticket_type_id": ga["id"], "full_name": "Aigerim N.", "email": "aigerim@example.com"},
        {"ticket_type_id": ga["id"], "full_name": "Me Myself", "email": "ATT-BUYER@example.com"},
        {"ticket_type_id": vip["id"], "full_name": "Daniyar K.", "email": "daniyar@example.com"},
    ]
    response = await _order(client, token, event["id"], [_item(ga, 2), _item(vip)],
                            attendees=attendees)
    assert response.status_code == 201, response.text
    await _pay(client, token, response.json()["id"])
    tickets = (await client.get(f"{API}/orders/{response.json()['id']}/tickets",
                                headers=bearer(token))).json()
    assert sorted(t["attendee_name"] for t in tickets) == ["Aigerim N.", "Daniyar K.", "Me Myself"]
    assert {t["attendee_email"] for t in tickets} == {
        "aigerim@example.com", "att-buyer@example.com", "daniyar@example.com"}

    wrong_count = await _order(client, token, event["id"], [_item(ga, 2)],
                               attendees=attendees[:1])
    assert wrong_count.status_code == 422
    wrong_type = await _order(client, token, event["id"], [_item(ga)], attendees=[
        {"ticket_type_id": vip["id"], "full_name": "X", "email": "x@example.com"}])
    assert wrong_type.status_code == 422
    blank = await _order(client, token, event["id"], [_item(ga)], attendees=[
        {"ticket_type_id": ga["id"], "full_name": "  ", "email": "x@example.com"}])
    assert blank.status_code == 422
    # Omitted -> buyer account, as before.
    plain = await _order(client, token, event["id"], [_item(vip)])
    await _pay(client, token, plain.json()["id"])
    plain_tickets = (await client.get(f"{API}/orders/{plain.json()['id']}/tickets",
                                      headers=bearer(token))).json()
    assert plain_tickets[0]["attendee_email"] == buyer.email


# ---------- 4. Audit-log filters, labels, COMPLETED ----------

async def test_audit_log_filters_and_labels(client, app, db):
    _owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="alog")
    await _campaign(client, owner_token, event["id"])
    _b, token = await _buyer(app, db, "alog-buyer@example.com")
    order = (await _order(client, token, event["id"], [_item(ga)])).json()
    await _pay(client, token, order["id"])

    path = f"{API}/events/{event['id']}/audit-log"
    entries = (await client.get(path, headers=bearer(owner_token))).json()
    order_entry = next(e for e in entries if e["action"] == "order.created")
    assert order_entry["entity_label"] == f"Order #{order['order_number']}"
    assert order_entry["actor_name"] == "Test User"
    campaign_entry = next(e for e in entries if e["action"] == "campaign.created")
    assert campaign_entry["entity_label"] == "Spring promo"

    prefixed = (await client.get(path, params={"action_prefix": "order."},
                                 headers=bearer(owner_token))).json()
    assert {e["action"] for e in prefixed} == {"order.created", "order.paid"}
    today = datetime.now(timezone.utc).date()
    in_range = (await client.get(path, params={"from": str(today - timedelta(days=1)),
                                               "to": str(today + timedelta(days=1))},
                                 headers=bearer(owner_token))).json()
    assert len(in_range) == len(entries)
    future = (await client.get(path, params={"from": str(today + timedelta(days=2))},
                               headers=bearer(owner_token))).json()
    assert future == []


async def test_finished_events_move_to_completed(client, app, db):
    _owner, token = await _create_owner(client, app, db, "done-owner@example.com")
    ended, _ = await _make_event(client, token, title="Yesterday")
    running, _ = await _make_event(client, token, title="Tomorrow")
    now = datetime.now(timezone.utc)
    await db.execute(update(Event).where(Event.id == UUID(ended["id"])).values(
        starts_at=now - timedelta(days=2), ends_at=now - timedelta(days=1)))
    await db.commit()

    assert await event_service.complete_finished_events(db) == 1
    await db.commit()
    assert await event_service.complete_finished_events(db) == 0  # idempotent
    db.expire_all()
    assert (await db.get(Event, UUID(ended["id"]))).status == EventStatus.COMPLETED
    assert (await db.get(Event, UUID(running["id"]))).status == EventStatus.PUBLISHED
    entry = await db.scalar(select(AuditLog).where(
        AuditLog.action == "event.completed", AuditLog.event_id == UUID(ended["id"])))
    assert entry is not None and entry.actor_type == "SYSTEM"
    history = (await client.get(f"{API}/organizers/me/events", params={"category": "COMPLETED"},
                                headers=bearer(token))).json()
    assert [e["title"] for e in history["items"]] == ["Yesterday"]


# ---------- 5. Support contract ----------

async def test_support_contract_aliases(client, app, db):
    owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="sc")
    buyer, buyer_token = await _buyer(app, db, "sc-buyer@example.com")
    order = (await _order(client, buyer_token, event["id"], [_item(ga)])).json()
    await _pay(client, buyer_token, order["id"])
    ticket = (await client.get(f"{API}/orders/{order['id']}/tickets",
                               headers=bearer(buyer_token))).json()[0]
    staff = await create_user(db, email="sc-staff@example.com", first_name="Staff")
    db.add(EventStaff(event_id=UUID(event["id"]), user_id=staff.id,
                      role=EventStaffRole.EVENT_ADMIN, permissions={}))
    await db.commit()
    staff_token = access_token_for(app, staff)
    _admin_user, admin_token = await _admin(app, db)

    # ticket_id may be the ticket's public id.
    created = await client.post(f"{API}/support/cases", json={
        "category": "EVENT_INFORMATION", "subject": "Seat question", "message": "Where do I sit?",
        "ticket_id": ticket["public_id"]}, headers=bearer(buyer_token))
    assert created.status_code == 201, created.text
    case = created.json()
    assert case["event"] == {"id": event["id"], "title": event["title"]}
    assert case["order"] == {"id": order["id"], "order_number": order["order_number"]}
    assert case["ticket"]["public_id"] == ticket["public_id"]
    assert case["ticket"]["ticket_type_name"] == ga["name"]
    assert case["requester"] == {"user_id": str(buyer.id), "display_name": "Test User",
                                 "name": "Test User"}
    assert case["ticket_public_id"] == ticket["public_id"]
    assert case["ticket_id"] == ticket["ticket_id"]
    platform = (await client.post(f"{API}/support/cases", json={
        "category": "ACCOUNT", "subject": "Activation help", "message": "Fee paid?"},
        headers=bearer(owner_token))).json()
    assert platform["case_type"] == "PLATFORM"

    async def scoped(token, scope):
        return await client.get(f"{API}/support/cases", params={"scope": scope},
                                headers=bearer(token))
    assert [c["id"] for c in (await scoped(buyer_token, "mine")).json()["items"]] == [case["id"]]
    assert [c["id"] for c in (await scoped(owner_token, "mine")).json()["items"]] == \
        [platform["id"]]
    assert [c["id"] for c in (await scoped(owner_token, "event")).json()["items"]] == \
        [case["id"]]
    assert [c["id"] for c in (await scoped(staff_token, "event")).json()["items"]] == \
        [case["id"]]
    assert (await scoped(buyer_token, "event")).json()["total"] == 0
    assert (await scoped(owner_token, "all")).status_code == 403
    assert [c["id"] for c in (await scoped(admin_token, "all")).json()["items"]] == \
        [platform["id"]]

    messages = f"{API}/support/cases/{case['id']}/messages"
    first = (await client.get(messages, headers=bearer(buyer_token))).json()["items"][0]
    assert first["sender"] == {"user_id": str(buyer.id), "display_name": "Test User",
                               "name": "Test User", "role": "REQUESTER"}
    reply = (await client.post(messages, json={"body": "Row 5"},
                               headers=bearer(owner_token))).json()
    polled = (await client.get(messages, params={"after": first["id"]},
                               headers=bearer(buyer_token))).json()
    assert [m["id"] for m in polled["items"]] == [reply["id"]]
    assert (await client.get(messages, params={"after": reply["id"]},
                             headers=bearer(buyer_token))).json()["items"] == []
    assert (await client.get(messages, params={"after": platform["id"]},
                             headers=bearer(buyer_token))).status_code == 404

    assignees = (await client.get(f"{API}/support/cases/{case['id']}/assignees",
                                  headers=bearer(owner_token))).json()
    assert {a["user_id"] for a in assignees} == {str(owner.id), str(staff.id)}
    assert all(a["name"] for a in assignees)

    patch_url = f"{API}/support/cases/{case['id']}"
    both = await client.patch(patch_url, json={"status": "IN_PROGRESS",
                                               "assignee_user_id": str(staff.id)},
                              headers=bearer(owner_token))
    assert both.status_code == 200, both.text
    assert both.json()["status"] == "IN_PROGRESS"
    assert both.json()["assignee"]["user_id"] == str(staff.id)
    only_status = await client.patch(patch_url, json={"status": "WAITING_FOR_CUSTOMER"},
                                     headers=bearer(staff_token))
    assert only_status.json()["assignee"]["user_id"] == str(staff.id)  # untouched
    unassign = await client.patch(patch_url, json={"assignee_user_id": None},
                                  headers=bearer(owner_token))
    assert unassign.json()["assignee"] is None
    assert unassign.json()["status"] == "WAITING_FOR_CUSTOMER"
    assert (await client.patch(patch_url, json={}, headers=bearer(owner_token))).status_code == 422
    # A rejected part rolls the whole request back.
    rejected = await client.patch(patch_url, json={"status": "IN_PROGRESS",
                                                   "assignee_user_id": str(buyer.id)},
                                  headers=bearer(owner_token))
    assert rejected.status_code == 422
    after = (await client.get(patch_url, headers=bearer(owner_token))).json()
    assert after["status"] == "WAITING_FOR_CUSTOMER" and after["assignee"] is None
    assert (await client.patch(patch_url, json={"assignee_user_id": str(staff.id)},
                               headers=bearer(buyer_token))).status_code == 403


# ---------- 6. Admin contract and CSV export ----------

async def test_admin_item_fields_campaigns_and_csv_export(client, app, db):
    owner, owner_token, event, ga, _vip = await _setup(client, app, db, prefix="adm")
    _admin_user, admin_token = await _admin(app, db)
    await _campaign(client, owner_token, event["id"], name="=HYPERLINK(\"x\")")
    _b, token = await _buyer(app, db, "adm-buyer@example.com")
    order = (await _order(client, token, event["id"], [_item(ga)])).json()
    await _pay(client, token, order["id"])

    async def get(path, **params):
        response = await client.get(f"{API}{path}", params=params, headers=bearer(admin_token))
        assert response.status_code == 200, response.text
        return response.json()

    users = await get("/admin/users", q="adm-buyer")
    assert set(users) >= {"items", "total"}
    assert users["items"][0]["id"] == users["items"][0]["user_id"]
    assert users["items"][0]["name"] == "Test User"
    events = await get("/admin/events", q=event["title"][:5])
    assert events["items"][0]["owner"]["id"] == str(owner.id)
    assert events["items"][0]["owner"]["name"]
    orders = await get("/admin/orders", q=str(order["order_number"]))
    assert orders["items"][0]["event"] == {"id": event["id"], "title": event["title"]}
    activations = await get("/admin/activations", q=event["title"][:5])
    [activation] = activations["items"]
    assert activation["event"]["id"] == event["id"]
    assert activation["organizer"]["id"] == str(owner.id)
    assert activation["checklist"] == {"verification_complete": True,
                                       "payout_account_complete": True,
                                       "activation_fee_paid": True, "terms_accepted": True}
    assert activation["status"] == "ACTIVE"
    campaigns = await get("/admin/campaigns", q="hyperlink")
    [campaign] = campaigns["items"]
    assert campaign["event"]["id"] == event["id"] and campaign["code"] == "SPRING10"
    assert campaign["effective_status"] == "ACTIVE" and campaign["redemption_count"] == 0
    await client.post(f"{API}/orders/{order['id']}/refund", headers=bearer(owner_token))
    refunds = await get("/admin/refunds", q=str(order["order_number"]))
    assert refunds["items"][0]["event"]["id"] == event["id"]

    for kind, expected in (("users", "adm-buyer@example.com"), ("events", event["id"]),
                           ("orders", order["id"]), ("payments", order["id"])):
        response = await client.get(f"{API}/admin/export/{kind}.csv",
                                    headers=bearer(admin_token))
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/csv")
        assert f'filename="{kind}.csv"' in response.headers["content-disposition"]
        rows = list(csv.reader(io.StringIO(response.text)))
        assert rows[0][0] == "id" and len(rows) >= 2
        assert expected in response.text
        assert "password" not in response.text.lower()
    filtered = await client.get(f"{API}/admin/export/users.csv", params={"q": "adm-buyer"},
                                headers=bearer(admin_token))
    assert len(list(csv.reader(io.StringIO(filtered.text)))) == 2
    assert (await client.get(f"{API}/admin/export/users.csv",
                             headers=bearer(owner_token))).status_code == 403
    assert (await client.get(f"{API}/admin/export/secrets.csv",
                             headers=bearer(admin_token))).status_code == 422
    exports = (await get("/admin/actions", action="admin.data_exported"))["total"]
    assert exports == 5


async def test_csv_cells_are_formula_safe():
    from app.services.admin import _csv_cell

    assert _csv_cell("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert _csv_cell("+1") == "'+1" and _csv_cell("@x") == "'@x"
    assert _csv_cell("plain") == "plain" and _csv_cell(None) == ""


# ---------- First Platform Admin ----------

async def test_bootstrap_first_platform_admin(client, app, db):
    import pytest

    from app.db.bootstrap_admin import BootstrapError, promote_to_platform_admin
    from app.db.models import User, UserStatus

    user = await create_user(db, email="first-admin@example.com")
    token = access_token_for(app, user)
    assert (await client.get(f"{API}/admin/users", headers=bearer(token))).status_code == 403
    assert await promote_to_platform_admin(db, "FIRST-ADMIN@example.com") is True
    await db.commit()
    assert await promote_to_platform_admin(db, "first-admin@example.com") is False  # idempotent
    # The role is re-read per request, so the existing token now works.
    assert (await client.get(f"{API}/admin/users", headers=bearer(token))).status_code == 200
    entry = await db.scalar(select(AuditLog).where(AuditLog.action == "admin.bootstrap_promoted"))
    assert entry is not None and entry.actor_type == "SYSTEM"

    await create_user(db, email="unverified@example.com", verified=False)
    await create_user(db, email="blocked@example.com", status=UserStatus.BLOCKED)
    for email in ("missing@example.com", "unverified@example.com", "blocked@example.com"):
        with pytest.raises(BootstrapError):
            await promote_to_platform_admin(db, email)
    roles = {u.email: u.global_role for u in (await db.execute(select(User))).scalars()}
    assert roles["unverified@example.com"] == UserRole.USER
