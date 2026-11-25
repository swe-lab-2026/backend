"""SCRUM-16: support cases, asynchronous messages, assignment, escalation,
relationship-based authorization and in-app notifications."""

from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select, update

from app.db.models import (
    AuditLog,
    EventStaff,
    EventStaffRole,
    Notification,
    SupportMessage,
    UserRole,
    UserStatus,
)
from tests.conftest import API, access_token_for, bearer, create_user
from tests.test_campaigns import _buyer, _item, _order, _pay, _setup

# ---------- Fixtures ----------

class World:
    """One paid event with a buyer, staff of every role and outsiders."""


async def _world(client, app, db, prefix: str) -> World:
    w = World()
    w.owner, w.owner_token, w.event, w.ga, w.vip = await _setup(client, app, db, prefix=prefix)
    w.buyer, w.buyer_token = await _buyer(app, db, f"{prefix}-buyer@example.com")
    order = await _order(client, w.buyer_token, w.event["id"], [_item(w.ga)])
    assert order.status_code == 201, order.text
    await _pay(client, w.buyer_token, order.json()["id"])
    w.order = order.json()
    tickets = await client.get(
        f"{API}/orders/{w.order['id']}/tickets", headers=bearer(w.buyer_token)
    )
    w.ticket = tickets.json()[0]

    async def staff(email: str, role: EventStaffRole):
        user = await create_user(db, email=email, first_name=role.value.title())
        db.add(EventStaff(event_id=UUID(w.event["id"]), user_id=user.id, role=role,
                          permissions={}))
        await db.commit()
        return user, access_token_for(app, user)

    w.admin_staff, w.admin_staff_token = await staff(
        f"{prefix}-evadmin@example.com", EventStaffRole.EVENT_ADMIN
    )
    w.org_staff, w.org_staff_token = await staff(
        f"{prefix}-orgstaff@example.com", EventStaffRole.ORGANIZER_STAFF
    )
    w.checkin, w.checkin_token = await staff(
        f"{prefix}-checkin@example.com", EventStaffRole.CHECK_IN_STAFF
    )
    w.other_owner, w.other_token, w.other_event, w.other_ga, _vip = await _setup(
        client, app, db, prefix=f"{prefix}-other"
    )
    w.outsider, w.outsider_token = await _buyer(app, db, f"{prefix}-outsider@example.com")
    w.platform = await create_user(
        db, email=f"{prefix}-padmin@example.com", role=UserRole.PLATFORM_ADMIN
    )
    w.platform_token = access_token_for(app, w.platform)
    return w


async def _open(client, token, **body):
    payload = {"subject": "Where is my ticket?", "category": "TICKET_DELIVERY",
               "message": "I did not receive the email."}
    payload.update(body)
    return await client.post(f"{API}/support/cases", json=payload, headers=bearer(token))


async def _attendee_case(client, w: World, **body):
    response = await _open(client, w.buyer_token, ticket_id=w.ticket["ticket_id"], **body)
    assert response.status_code == 201, response.text
    return response.json()


async def _notifications(db, user_id, kind=None):
    # populate_existing instead of expire_all: fixtures keep using the loaded
    # user objects (an expired attribute would lazy-load outside the loop).
    stmt = select(Notification).where(Notification.user_id == user_id)
    if kind:
        stmt = stmt.where(Notification.type == kind)
    stmt = stmt.execution_options(populate_existing=True)
    return list((await db.execute(stmt)).scalars().all())


async def _get(client, token, path, **params):
    return await client.get(f"{API}{path}", headers=bearer(token), params=params)


# ---------- Creation and context resolution ----------

async def test_case_from_ticket_resolves_order_and_event(client, app, db):
    w = await _world(client, app, db, "ctx")
    case = await _attendee_case(client, w)
    assert case["case_type"] == "ATTENDEE_ORGANIZER"
    assert case["ticket_id"] == w.ticket["ticket_id"]
    assert case["order_id"] == w.order["id"]  # resolved from the ticket
    assert case["event_id"] == w.event["id"]  # resolved from the order
    assert case["event_title"] == w.event["title"]
    assert case["order_number"] == w.order["order_number"]
    assert case["status"] == "OPEN"
    assert case["message_count"] == 1
    assert case["requester"]["user_id"] == str(w.buyer.id)
    assert case["access"] == {"relation": "REQUESTER", "can_manage": False,
                              "can_escalate": True}

    from_order = await _open(client, w.buyer_token, category="PAYMENT", order_id=w.order["id"])
    assert from_order.status_code == 201, from_order.text
    assert from_order.json()["event_id"] == w.event["id"]
    assert from_order.json()["ticket_id"] is None

    from_event = await _open(client, w.buyer_token, category="EVENT_INFORMATION",
                             event_id=w.event["id"])
    assert from_event.status_code == 201, from_event.text
    assert from_event.json()["order_id"] is None

    # Fully consistent explicit context is accepted too.
    explicit = await _open(client, w.buyer_token, ticket_id=w.ticket["ticket_id"],
                           order_id=w.order["id"], event_id=w.event["id"])
    assert explicit.status_code == 201, explicit.text

    # Audit trail entry for creation, tied to the event.
    created = await db.scalar(
        select(AuditLog).where(AuditLog.action == "support_case.created",
                               AuditLog.entity_id == UUID(case["id"]))
    )
    assert created is not None and str(created.event_id) == w.event["id"]

    # Account/technical issue without any event context goes to the platform.
    general = await _open(client, w.buyer_token, category="ACCOUNT", subject="Cannot log in")
    assert general.status_code == 201
    assert general.json()["case_type"] == "PLATFORM"
    assert general.json()["event_id"] is None


async def test_invalid_and_inconsistent_context_is_rejected(client, app, db):
    w = await _world(client, app, db, "badctx")
    # Second order of the same buyer, for mismatch checks.
    second = await _order(client, w.buyer_token, w.event["id"], [_item(w.vip)])
    await _pay(client, w.buyer_token, second.json()["id"])

    cases = [
        ({"ticket_id": str(uuid4())}, 404, "SUPPORT_CONTEXT_NOT_FOUND"),
        ({"order_id": str(uuid4())}, 404, "SUPPORT_CONTEXT_NOT_FOUND"),
        ({"event_id": str(uuid4())}, 404, "SUPPORT_CONTEXT_NOT_FOUND"),
        # Ticket of the buyer + a different order of the same buyer.
        ({"ticket_id": w.ticket["ticket_id"], "order_id": second.json()["id"]}, 422,
         "SUPPORT_CONTEXT_MISMATCH"),
        # Own order + another organizer's event.
        ({"order_id": w.order["id"], "event_id": w.other_event["id"]}, 422,
         "SUPPORT_CONTEXT_MISMATCH"),
        ({"case_type": "ATTENDEE_ORGANIZER"}, 422, "SUPPORT_CONTEXT_REQUIRED"),
    ]
    for body, status, code in cases:
        response = await _open(client, w.buyer_token, **body)
        assert response.status_code == status, (body, response.text)
        assert response.json()["error"]["code"] == code

    # Someone else's ticket/order: indistinguishable from a missing one.
    for body in ({"ticket_id": w.ticket["ticket_id"]}, {"order_id": w.order["id"]}):
        response = await _open(client, w.outsider_token, **body)
        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "SUPPORT_CONTEXT_NOT_FOUND"

    # Organizer cannot open an attendee case on their own event...
    own = await _open(client, w.owner_token, case_type="ATTENDEE_ORGANIZER",
                      event_id=w.event["id"])
    assert own.status_code == 422
    assert own.json()["error"]["code"] == "SUPPORT_ROUTE_INVALID"
    # ...and nobody but the owner opens a platform case about an event.
    foreign = await _open(client, w.other_token, case_type="PLATFORM", event_id=w.event["id"])
    assert foreign.status_code == 404

    # Enum and content validation.
    for body in ({"category": "Refunds please"}, {"message": "   "}, {"subject": "  "}):
        response = await _open(client, w.buyer_token, **body)
        assert response.status_code == 422, body


# ---------- Relationship-based authorization ----------

async def test_attendee_case_access_matrix(client, app, db):
    w = await _world(client, app, db, "acl")
    case = await _attendee_case(client, w)
    path = f"/support/cases/{case['id']}"

    for token, relation in (
        (w.buyer_token, "REQUESTER"),
        (w.owner_token, "ORGANIZER"),
        (w.admin_staff_token, "STAFF"),
        (w.org_staff_token, "STAFF"),
    ):
        response = await _get(client, token, path)
        assert response.status_code == 200, response.text
        assert response.json()["access"]["relation"] == relation

    # No relationship: 403 and no case metadata in the body.
    for token in (w.checkin_token, w.other_token, w.outsider_token, w.platform_token):
        for method, sub, body in (
            ("GET", "", None),
            ("GET", "/messages", None),
            ("GET", "/activity", None),
            ("POST", "/messages", {"body": "hi"}),
            ("PATCH", "/status", {"status": "RESOLVED"}),
            ("PATCH", "/assignee", {"assignee_user_id": None}),
            ("POST", "/escalate", {"reason": "please help"}),
        ):
            response = await client.request(
                method, f"{API}{path}{sub}", json=body, headers=bearer(token)
            )
            assert response.status_code == 403, (method, sub, response.text)
            assert response.json()["error"]["code"] == "SUPPORT_CASE_ACCESS_DENIED"
            assert case["subject"] not in response.text
            assert response.json()["error"]["details"] == {}

    missing = await _get(client, w.owner_token, f"/support/cases/{uuid4()}")
    assert missing.status_code == 404

    # Listings exclude inaccessible cases entirely (and their counts).
    for token, expected in (
        (w.buyer_token, 1), (w.owner_token, 1), (w.admin_staff_token, 1),
        (w.checkin_token, 0), (w.other_token, 0), (w.outsider_token, 0),
        (w.platform_token, 0),
    ):
        listed = await _get(client, token, "/support/cases")
        assert listed.status_code == 200
        assert listed.json()["total"] == expected, token
        assert len(listed.json()["items"]) == expected

    # Removing the staff assignment revokes access immediately.
    await db.execute(
        EventStaff.__table__.delete().where(EventStaff.user_id == w.admin_staff.id)
    )
    await db.commit()
    revoked = await _get(client, w.admin_staff_token, path)
    assert revoked.status_code == 403


async def test_platform_case_is_only_for_organizer_and_admins(client, app, db):
    w = await _world(client, app, db, "plat")
    # Event context on the organizer's own event defaults to PLATFORM routing.
    response = await _open(client, w.owner_token, category="PAYMENT",
                           subject="Payout question", event_id=w.event["id"])
    assert response.status_code == 201, response.text
    case = response.json()
    assert case["case_type"] == "PLATFORM"
    path = f"/support/cases/{case['id']}"

    admin_view = await _get(client, w.platform_token, path)
    assert admin_view.status_code == 200
    assert admin_view.json()["access"]["relation"] == "PLATFORM_ADMIN"
    reply = await client.post(f"{API}{path}/messages", json={"body": "Looking into it"},
                              headers=bearer(w.platform_token))
    assert reply.status_code == 201
    assert reply.json()["sender"]["role"] == "PLATFORM_ADMIN"

    # Event staff and customers of that event have no access to it.
    for token in (w.admin_staff_token, w.org_staff_token, w.buyer_token, w.other_token):
        assert (await _get(client, token, path)).status_code == 403
        assert (await _get(client, token, "/support/cases",
                           case_type="PLATFORM")).json()["total"] == 0

    admin_list = await _get(client, w.platform_token, "/support/cases", case_type="PLATFORM")
    assert [c["id"] for c in admin_list.json()["items"]] == [case["id"]]


# ---------- Messages: chronology, polling, pagination ----------

async def test_messages_are_chronological_with_polling_and_pagination(client, app, db):
    w = await _world(client, app, db, "msg")
    case = await _attendee_case(client, w, message="m1")
    path = f"{API}/support/cases/{case['id']}/messages"
    for i, token in enumerate(
        [w.owner_token, w.buyer_token, w.admin_staff_token, w.buyer_token, w.owner_token],
        start=2,
    ):
        sent = await client.post(path, json={"body": f"m{i}"}, headers=bearer(token))
        assert sent.status_code == 201, sent.text
        assert sent.json()["sequence"] == i

    # Identical timestamps must not change the order: sequence is authoritative.
    same = datetime(2026, 1, 1, tzinfo=timezone.utc)
    await db.execute(update(SupportMessage).where(
        SupportMessage.case_id == UUID(case["id"])).values(created_at=same))
    await db.commit()

    full = (await client.get(path, headers=bearer(w.buyer_token))).json()
    assert [m["body"] for m in full["items"]] == [f"m{i}" for i in range(1, 7)]
    assert [m["sender"]["role"] for m in full["items"]] == [
        "REQUESTER", "ORGANIZER", "REQUESTER", "STAFF", "REQUESTER", "ORGANIZER"]
    assert full["items"][1]["sender"]["name"]
    assert full["latest_sequence"] == 6 and full["has_more"] is False

    page1 = (await client.get(path, params={"limit": 2}, headers=bearer(w.owner_token))).json()
    assert [m["sequence"] for m in page1["items"]] == [1, 2]
    assert page1["has_more"] is True and page1["next_cursor"] == 2
    page2 = (await client.get(path, params={"limit": 2, "after_sequence": 2},
                              headers=bearer(w.owner_token))).json()
    assert [m["sequence"] for m in page2["items"]] == [3, 4]

    # Polling with the latest cursor: nothing new, cursor is kept.
    idle = (await client.get(path, params={"after_sequence": 6},
                             headers=bearer(w.buyer_token))).json()
    assert idle["items"] == [] and idle["next_cursor"] == 6 and idle["has_more"] is False
    await client.post(path, json={"body": "m7"}, headers=bearer(w.owner_token))
    fresh = (await client.get(path, params={"after_sequence": 6},
                              headers=bearer(w.buyer_token))).json()
    assert [m["body"] for m in fresh["items"]] == ["m7"] and fresh["next_cursor"] == 7

    # Backwards history paging stays oldest-first within the page.
    back = (await client.get(path, params={"before_sequence": 5, "limit": 2},
                             headers=bearer(w.buyer_token))).json()
    assert [m["sequence"] for m in back["items"]] == [3, 4]
    assert back["has_more"] is True and back["prev_cursor"] == 3

    # Timestamp-based polling.
    since = (await client.get(path, params={"since": same.isoformat()},
                              headers=bearer(w.buyer_token))).json()
    assert [m["body"] for m in since["items"]] == ["m7"]

    both = await client.get(path, params={"after_sequence": 1, "before_sequence": 3},
                            headers=bearer(w.buyer_token))
    assert both.status_code == 422
    blank = await client.post(path, json={"body": "   "}, headers=bearer(w.buyer_token))
    assert blank.status_code == 422
    too_long = await client.post(path, json={"body": "x" * 5001},
                                 headers=bearer(w.buyer_token))
    assert too_long.status_code == 422


# ---------- Lifecycle ----------

async def test_status_transitions_permissions_and_history(client, app, db):
    w = await _world(client, app, db, "life")
    case = await _attendee_case(client, w)
    base = f"{API}/support/cases/{case['id']}"

    async def status(token, value, **extra):
        return await client.patch(f"{base}/status", json={"status": value, **extra},
                                  headers=bearer(token))

    assert (await status(w.owner_token, "IN_PROGRESS")).json()["status"] == "IN_PROGRESS"
    waiting = await status(w.admin_staff_token, "WAITING_FOR_CUSTOMER", note="Need order no.")
    assert waiting.json()["status"] == "WAITING_FOR_CUSTOMER"

    # The requester cannot drive staff states.
    denied = await status(w.buyer_token, "IN_PROGRESS")
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "SUPPORT_STATUS_FORBIDDEN"

    # A customer reply hands the case back to staff automatically.
    await client.post(f"{base}/messages", json={"body": "Order 42"},
                      headers=bearer(w.buyer_token))
    assert (await client.get(base, headers=bearer(w.buyer_token))).json()["status"] == \
        "IN_PROGRESS"

    invalid = await status(w.owner_token, "OPEN")
    assert invalid.status_code == 409
    assert invalid.json()["error"]["code"] == "INVALID_STATUS_TRANSITION"

    resolved = await status(w.owner_token, "RESOLVED")
    assert resolved.json()["status"] == "RESOLVED"
    assert resolved.json()["resolved_at"] is not None
    assert (await status(w.owner_token, "RESOLVED")).status_code == 200  # idempotent

    closed_msg = await client.post(f"{base}/messages", json={"body": "one more"},
                                   headers=bearer(w.buyer_token))
    assert closed_msg.status_code == 409
    assert closed_msg.json()["error"]["code"] == "SUPPORT_CASE_RESOLVED"

    reopened = await status(w.buyer_token, "OPEN")
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "OPEN" and reopened.json()["resolved_at"] is None
    # The requester may also resolve their own case.
    assert (await status(w.buyer_token, "RESOLVED")).json()["status"] == "RESOLVED"

    activity = (await client.get(f"{base}/activity", headers=bearer(w.buyer_token))).json()
    steps = [(a["action"], a["from_status"], a["to_status"]) for a in activity["items"]]
    assert steps == [
        ("support_case.created", None, "OPEN"),
        ("support_case.status_changed", "OPEN", "IN_PROGRESS"),
        ("support_case.status_changed", "IN_PROGRESS", "WAITING_FOR_CUSTOMER"),
        ("support_case.status_changed", "WAITING_FOR_CUSTOMER", "IN_PROGRESS"),
        ("support_case.status_changed", "IN_PROGRESS", "RESOLVED"),
        ("support_case.status_changed", "RESOLVED", "OPEN"),
        ("support_case.status_changed", "OPEN", "RESOLVED"),
    ]
    actors = [a["actor"]["user_id"] for a in activity["items"]]
    assert actors[1] == str(w.owner.id) and actors[2] == str(w.admin_staff.id)
    assert actors[3] == str(w.buyer.id)
    assert activity["items"][2]["note"] == "Need order no."
    timestamps = [a["timestamp"] for a in activity["items"]]
    assert timestamps == sorted(timestamps)


# ---------- Assignment ----------

async def test_assignment_respects_event_level_permissions(client, app, db):
    w = await _world(client, app, db, "asg")
    case = await _attendee_case(client, w)
    base = f"{API}/support/cases/{case['id']}"

    async def assign(token, user_id):
        return await client.patch(
            f"{base}/assignee",
            json={"assignee_user_id": str(user_id) if user_id else None},
            headers=bearer(token),
        )

    assignable = await client.get(f"{base}/assignable-users", headers=bearer(w.owner_token))
    assert {u["user_id"] for u in assignable.json()} == {
        str(w.owner.id), str(w.admin_staff.id), str(w.org_staff.id)}

    ok = await assign(w.owner_token, w.admin_staff.id)
    assert ok.status_code == 200, ok.text
    assert ok.json()["assignee"]["user_id"] == str(w.admin_staff.id)

    # Arbitrary users are never assignable.
    for target in (w.checkin.id, w.other_owner.id, w.outsider.id, w.platform.id, uuid4(),
                   w.buyer.id):
        response = await assign(w.owner_token, target)
        assert response.status_code == 422, target
        assert response.json()["error"]["code"] == "SUPPORT_ASSIGNEE_NOT_ELIGIBLE"

    # The requester cannot assign; staff can reassign; unassign works.
    assert (await assign(w.buyer_token, w.owner.id)).status_code == 403
    reassigned = await assign(w.admin_staff_token, w.org_staff.id)
    assert reassigned.json()["assignee"]["user_id"] == str(w.org_staff.id)
    unassigned = await assign(w.org_staff_token, None)
    assert unassigned.json()["assignee"] is None

    # A blocked staff member is not assignable.
    blocked = await create_user(db, email="asg-blocked@example.com",
                                status=UserStatus.BLOCKED)
    db.add(EventStaff(event_id=UUID(w.event["id"]), user_id=blocked.id,
                      role=EventStaffRole.EVENT_ADMIN, permissions={}))
    await db.commit()
    assert (await assign(w.owner_token, blocked.id)).status_code == 422

    # Platform Admins become assignable only after escalation.
    await client.post(f"{base}/escalate", json={"reason": "No answer for days"},
                      headers=bearer(w.buyer_token))
    escalated = await assign(w.platform_token, w.platform.id)
    assert escalated.status_code == 200, escalated.text

    actions = [a["action"] for a in (await client.get(
        f"{base}/activity", headers=bearer(w.owner_token))).json()["items"]]
    assert actions.count("support_case.assigned") == 3
    assert actions.count("support_case.unassigned") == 1

    # Platform cases: only Platform Admins are assignable.
    platform_case = (await _open(client, w.owner_token, category="TECHNICAL")).json()
    p_base = f"{API}/support/cases/{platform_case['id']}"
    other_admin = await create_user(db, email="asg-admin2@example.com",
                                    role=UserRole.PLATFORM_ADMIN)
    assert (await client.patch(f"{p_base}/assignee",
                               json={"assignee_user_id": str(other_admin.id)},
                               headers=bearer(w.platform_token))).status_code == 200
    assert (await client.patch(f"{p_base}/assignee",
                               json={"assignee_user_id": str(w.owner.id)},
                               headers=bearer(w.platform_token))).status_code == 422
    # The organizer (requester) cannot assign their own platform case.
    assert (await client.patch(f"{p_base}/assignee",
                               json={"assignee_user_id": str(other_admin.id)},
                               headers=bearer(w.owner_token))).status_code == 403


# ---------- Notifications ----------

async def test_notifications_reach_only_authorized_participants(client, app, db):
    w = await _world(client, app, db, "ntf")
    case = await _attendee_case(client, w)
    base = f"{API}/support/cases/{case['id']}"
    created = "SUPPORT_CASE_CREATED"

    for user in (w.owner, w.admin_staff, w.org_staff):
        rows = await _notifications(db, user.id, created)
        assert len(rows) == 1 and rows[0].payload["case_id"] == case["id"]
        assert rows[0].channel.value == "IN_APP"
    for user in (w.buyer, w.checkin, w.other_owner, w.outsider, w.platform):
        assert await _notifications(db, user.id) == []  # no self/unauthorized notices

    # Staff reply -> requester only.
    await client.post(f"{base}/messages", json={"body": "On it"},
                      headers=bearer(w.org_staff_token))
    assert len(await _notifications(db, w.buyer.id, "SUPPORT_MESSAGE_RECEIVED")) == 1
    assert await _notifications(db, w.org_staff.id, "SUPPORT_MESSAGE_RECEIVED") == []

    # Assignment -> assignee; requester message then reaches only the assignee.
    await client.patch(f"{base}/assignee", json={"assignee_user_id": str(w.admin_staff.id)},
                       headers=bearer(w.owner_token))
    assert len(await _notifications(db, w.admin_staff.id, "SUPPORT_CASE_ASSIGNED")) == 1
    await client.post(f"{base}/messages", json={"body": "Thanks"},
                      headers=bearer(w.buyer_token))
    assert len(await _notifications(db, w.admin_staff.id, "SUPPORT_MESSAGE_RECEIVED")) == 1
    assert await _notifications(db, w.owner.id, "SUPPORT_MESSAGE_RECEIVED") == []

    # Reassignment notifies the new and the previous assignee once each.
    await client.patch(f"{base}/assignee", json={"assignee_user_id": str(w.org_staff.id)},
                       headers=bearer(w.owner_token))
    assert len(await _notifications(db, w.org_staff.id, "SUPPORT_CASE_ASSIGNED")) == 1
    assert len(await _notifications(db, w.admin_staff.id, "SUPPORT_CASE_UNASSIGNED")) == 1

    # Self-assignment is not notified.
    await client.patch(f"{base}/assignee", json={"assignee_user_id": str(w.owner.id)},
                       headers=bearer(w.owner_token))
    assert await _notifications(db, w.owner.id, "SUPPORT_CASE_ASSIGNED") == []

    # Significant status changes reach the requester; resolution has its own kind.
    await client.patch(f"{base}/status", json={"status": "WAITING_FOR_CUSTOMER"},
                       headers=bearer(w.owner_token))
    assert len(await _notifications(db, w.buyer.id, "SUPPORT_CASE_STATUS_CHANGED")) == 1
    await client.patch(f"{base}/status", json={"status": "RESOLVED"},
                       headers=bearer(w.owner_token))
    assert len(await _notifications(db, w.buyer.id, "SUPPORT_CASE_RESOLVED")) == 1

    # A staff member who lost access is no longer notified.
    await db.execute(EventStaff.__table__.delete().where(EventStaff.user_id == w.org_staff.id))
    await db.commit()
    second = await _attendee_case(client, w, subject="Another question")
    assert len(await _notifications(db, w.org_staff.id, created)) == 1  # only the first
    assert len(await _notifications(db, w.owner.id, created)) == 2
    del second

    # The requester reads their inbox; foreign notifications are not readable.
    inbox = await client.get(f"{API}/users/me/notifications", headers=bearer(w.buyer_token))
    assert inbox.status_code == 200
    body = inbox.json()
    assert body["total"] == body["unread_count"] == 3
    first_id = body["items"][0]["id"]
    read = await client.post(f"{API}/users/me/notifications/{first_id}/read",
                             headers=bearer(w.buyer_token))
    assert read.status_code == 200 and read.json()["read_at"] is not None
    again = await client.post(f"{API}/users/me/notifications/{first_id}/read",
                              headers=bearer(w.buyer_token))
    assert again.json()["read_at"] == read.json()["read_at"]
    foreign = await client.post(f"{API}/users/me/notifications/{first_id}/read",
                                headers=bearer(w.owner_token))
    assert foreign.status_code == 404
    unread = await client.get(f"{API}/users/me/notifications",
                              params={"unread_only": True}, headers=bearer(w.buyer_token))
    assert unread.json()["total"] == 2


# ---------- Escalation ----------

async def test_escalation_makes_case_visible_to_platform_admins(client, app, db):
    w = await _world(client, app, db, "esc")
    case = await _attendee_case(client, w)
    base = f"{API}/support/cases/{case['id']}"

    assert (await client.get(f"{API}/admin/support/escalated",
                             headers=bearer(w.platform_token))).json()["total"] == 0
    # Check-in staff have no access, so they cannot escalate.
    assert (await client.post(f"{base}/escalate", json={"reason": "help"},
                              headers=bearer(w.checkin_token))).status_code == 403

    escalated = await client.post(f"{base}/escalate", json={"reason": "Organizer silent"},
                                  headers=bearer(w.buyer_token))
    assert escalated.status_code == 200, escalated.text
    assert escalated.json()["is_escalated"] is True
    assert escalated.json()["escalation_reason"] == "Organizer silent"

    again = await client.post(f"{base}/escalate", json={"reason": "again"},
                              headers=bearer(w.owner_token))
    assert again.status_code == 409

    monitored = await client.get(f"{API}/admin/support/escalated",
                                 headers=bearer(w.platform_token))
    assert [c["id"] for c in monitored.json()["items"]] == [case["id"]]
    assert (await client.get(f"{API}/admin/support/escalated",
                             headers=bearer(w.owner_token))).status_code == 403

    view = await client.get(base, headers=bearer(w.platform_token))
    assert view.status_code == 200
    assert view.json()["access"]["relation"] == "PLATFORM_ADMIN"
    assert len(await _notifications(db, w.platform.id, "SUPPORT_CASE_ESCALATED")) == 1
    assert len(await _notifications(db, w.owner.id, "SUPPORT_CASE_ESCALATED")) == 1
    reply = await client.post(f"{base}/messages", json={"body": "Platform here"},
                              headers=bearer(w.platform_token))
    assert reply.status_code == 201

    # Organizer staff still see it; other organizers still do not.
    assert (await client.get(base, headers=bearer(w.admin_staff_token))).status_code == 200
    assert (await client.get(base, headers=bearer(w.other_token))).status_code == 403

    platform_case = (await _open(client, w.owner_token, category="TECHNICAL")).json()
    not_escalatable = await client.post(
        f"{API}/support/cases/{platform_case['id']}/escalate", json={"reason": "why"},
        headers=bearer(w.owner_token))
    assert not_escalatable.status_code == 409

    filtered = await client.get(f"{API}/support/cases", params={"escalated": True},
                                headers=bearer(w.owner_token))
    assert [c["id"] for c in filtered.json()["items"]] == [case["id"]]


async def test_suspended_user_cannot_use_support(client, app, db):
    w = await _world(client, app, db, "susp")
    case = await _attendee_case(client, w)
    w.buyer.status = UserStatus.BLOCKED
    db.add(w.buyer)
    await db.commit()
    # The previously issued access token is rejected on the next request.
    response = await client.get(f"{API}/support/cases/{case['id']}",
                                headers=bearer(w.buyer_token))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "USER_BLOCKED"
    # A blocked requester is not notified of staff replies.
    await client.post(f"{API}/support/cases/{case['id']}/messages", json={"body": "hello"},
                      headers=bearer(w.owner_token))
    assert await _notifications(db, w.buyer.id) == []
