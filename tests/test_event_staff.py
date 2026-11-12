from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.api import deps
from app.core.errors import ApiError
from app.db.models import (
    AuditLog,
    Event,
    EventStaff,
    EventStaffRole,
    UserRole,
    UserStatus,
)
from tests.conftest import API, access_token_for, bearer, create_event, create_user

OWNER_EMAIL = "owner@example.com"
ADMIN_EMAIL = "admin@example.com"
STRANGER_EMAIL = "stranger@example.com"

EVENT_ADMIN_PERMISSIONS = {"view_event": True, "check_in_tickets": True}


async def _audit_entries(db, action: str):
    stmt = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id.asc())
    return (await db.execute(stmt)).scalars().all()


async def _setup_owner(client, app, db, *, email: str = OWNER_EMAIL):
    owner = await create_user(db, email=email)
    owner_token = access_token_for(app, owner)
    resp = await client.post(
        f"{API}/organizers/me",
        json={"display_name": "Owner Org", "terms_accepted": True},
        headers=bearer(owner_token),
    )
    assert resp.status_code == 201
    return owner, owner_token


async def _create_event_at(db, *, owner_user_id, title: str, starts_at: datetime) -> Event:
    event = Event(
        owner_user_id=owner_user_id,
        title=title,
        timezone="UTC",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=3),
    )
    db.add(event)
    await db.commit()
    return event


async def _assign_staff(db, *, event_id, user_id, role: EventStaffRole) -> None:
    db.add(
        EventStaff(
            event_id=event_id,
            user_id=user_id,
            role=role,
            permissions={},
        )
    )
    await db.commit()


async def _assign(client, token, event_id, email):
    return await client.post(
        f"{API}/events/{event_id}/admins", json={"email": email}, headers=bearer(token)
    )


async def _remove(client, token, event_id, user_id):
    return await client.delete(
        f"{API}/events/{event_id}/admins/{user_id}", headers=bearer(token)
    )


# 14. Owner assigns an active, verified user ----------------------------------
async def test_owner_assigns_event_admin(client, app, db):
    owner, owner_token = await _setup_owner(client, app, db)
    event = await create_event(db, owner_user_id=owner.id)
    admin = await create_user(db, email=ADMIN_EMAIL)

    resp = await _assign(client, owner_token, event.id, ADMIN_EMAIL)
    assert resp.status_code == 201
    body = resp.json()
    assert body["user_id"] == str(admin.id)
    assert body["role"] == "EVENT_ADMIN"
    assert body["permissions"] == EVENT_ADMIN_PERMISSIONS


# 15. Non-owner gets 403 -------------------------------------------------------
async def test_non_owner_cannot_assign(client, app, db):
    owner, _ = await _setup_owner(client, app, db)
    event = await create_event(db, owner_user_id=owner.id)
    stranger = await create_user(db, email=STRANGER_EMAIL)

    resp = await _assign(client, access_token_for(app, stranger), event.id, ADMIN_EMAIL)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "INSUFFICIENT_EVENT_PERMISSIONS"


# 16. Event Admin cannot assign another admin ----------------------------------
async def test_event_admin_cannot_assign(client, app, db):
    owner, owner_token = await _setup_owner(client, app, db)
    event = await create_event(db, owner_user_id=owner.id)
    admin = await create_user(db, email=ADMIN_EMAIL)
    assert (await _assign(client, owner_token, event.id, ADMIN_EMAIL)).status_code == 201

    other = await create_user(db, email="other-admin@example.com")
    resp = await _assign(client, access_token_for(app, admin), event.id, other.email)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "INSUFFICIENT_EVENT_PERMISSIONS"


# 17. PLATFORM_ADMIN can assign -------------------------------------------------
async def test_platform_admin_can_assign(client, app, db):
    owner, _ = await _setup_owner(client, app, db)
    event = await create_event(db, owner_user_id=owner.id)
    platform = await create_user(db, email="platform@example.com", role=UserRole.PLATFORM_ADMIN)
    await create_user(db, email=ADMIN_EMAIL)

    resp = await _assign(client, access_token_for(app, platform), event.id, ADMIN_EMAIL)
    assert resp.status_code == 201
    assert resp.json()["role"] == "EVENT_ADMIN"


# 18. Unknown target user ------------------------------------------------------
async def test_assign_unknown_user(client, app, db):
    owner, owner_token = await _setup_owner(client, app, db)
    event = await create_event(db, owner_user_id=owner.id)
    resp = await _assign(client, owner_token, event.id, "nobody@example.com")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "TARGET_USER_NOT_FOUND"


# 19. Blocked target user -------------------------------------------------------
async def test_assign_blocked_user(client, app, db):
    owner, owner_token = await _setup_owner(client, app, db)
    event = await create_event(db, owner_user_id=owner.id)
    await create_user(db, email=ADMIN_EMAIL, status=UserStatus.BLOCKED)

    resp = await _assign(client, owner_token, event.id, ADMIN_EMAIL)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "TARGET_USER_INACTIVE"


# 20. Unverified target user ------------------------------------------------------
async def test_assign_unverified_user(client, app, db):
    owner, owner_token = await _setup_owner(client, app, db)
    event = await create_event(db, owner_user_id=owner.id)
    await create_user(db, email=ADMIN_EMAIL, verified=False)

    resp = await _assign(client, owner_token, event.id, ADMIN_EMAIL)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "TARGET_USER_INACTIVE"


# 21. Owner cannot be an admin of their own event ---------------------------------
async def test_assign_owner_forbidden(client, app, db):
    owner, owner_token = await _setup_owner(client, app, db)
    event = await create_event(db, owner_user_id=owner.id)

    resp = await _assign(client, owner_token, event.id, OWNER_EMAIL)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "EVENT_OWNER_CANNOT_BE_ADMIN"


# 22. Duplicate assignment ---------------------------------------------------------
async def test_duplicate_assignment(client, app, db):
    owner, owner_token = await _setup_owner(client, app, db)
    event = await create_event(db, owner_user_id=owner.id)
    await create_user(db, email=ADMIN_EMAIL)
    assert (await _assign(client, owner_token, event.id, ADMIN_EMAIL)).status_code == 201

    resp = await _assign(client, owner_token, event.id, ADMIN_EMAIL)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "EVENT_ADMIN_ALREADY_ASSIGNED"


# 23. Assignment is audited ---------------------------------------------------------
async def test_assignment_audited(client, app, db):
    owner, owner_token = await _setup_owner(client, app, db)
    event = await create_event(db, owner_user_id=owner.id)
    admin = await create_user(db, email=ADMIN_EMAIL)
    await _assign(client, owner_token, event.id, ADMIN_EMAIL)

    entries = await _audit_entries(db, "event.admin_assigned")
    assert len(entries) == 1
    assert entries[0].event_id == event.id
    assert entries[0].entity_id == admin.id
    assert entries[0].entity_type == "event_staff"
    assert entries[0].after_data == {
        "role": "EVENT_ADMIN",
        "permissions": EVENT_ADMIN_PERMISSIONS,
    }


# 24/25. Event Admin sees only assigned events --------------------------------------
async def test_admin_sees_only_assigned_events(client, app, db):
    owner, owner_token = await _setup_owner(client, app, db)
    assigned_event = await create_event(db, owner_user_id=owner.id, title="Assigned")
    other_event = await create_event(db, owner_user_id=owner.id, title="Not Assigned")
    admin = await create_user(db, email=ADMIN_EMAIL)
    await _assign(client, owner_token, assigned_event.id, ADMIN_EMAIL)

    resp = await client.get(
        f"{API}/events/assigned", headers=bearer(access_token_for(app, admin))
    )
    assert resp.status_code == 200
    ids = {item["event_id"] for item in resp.json()}
    assert ids == {str(assigned_event.id)}
    assert str(other_event.id) not in ids


# 26. Assignment to event A does not grant access to event B ------------------------
async def test_assignment_does_not_span_events(client, app, db):
    owner, owner_token = await _setup_owner(client, app, db)
    event_a = await create_event(db, owner_user_id=owner.id, title="A")
    event_b = await create_event(db, owner_user_id=owner.id, title="B")
    admin = await create_user(db, email=ADMIN_EMAIL)
    await _assign(client, owner_token, event_a.id, ADMIN_EMAIL)

    # Allowed on A, denied on B.
    await deps.require_event_checkin_access(event_id=event_a.id, user=admin, db=db)
    with pytest.raises(ApiError) as exc:
        await deps.require_event_checkin_access(event_id=event_b.id, user=admin, db=db)
    assert exc.value.status_code == 403
    assert exc.value.code == "INSUFFICIENT_EVENT_PERMISSIONS"


# 27. Owner removes an Event Admin --------------------------------------------------
async def test_owner_removes_event_admin(client, app, db):
    owner, owner_token = await _setup_owner(client, app, db)
    event = await create_event(db, owner_user_id=owner.id)
    admin = await create_user(db, email=ADMIN_EMAIL)
    await _assign(client, owner_token, event.id, ADMIN_EMAIL)

    resp = await _remove(client, owner_token, event.id, admin.id)
    assert resp.status_code == 204

    # The (event_id, user_id) row is gone; assignment on the same event is now free.
    resp = await _assign(client, owner_token, event.id, ADMIN_EMAIL)
    assert resp.status_code == 201


# 28. Removal on one event does not affect another -----------------------------------
async def test_removal_does_not_affect_other_event(client, app, db):
    owner, owner_token = await _setup_owner(client, app, db)
    event_a = await create_event(db, owner_user_id=owner.id, title="A")
    event_b = await create_event(db, owner_user_id=owner.id, title="B")
    admin = await create_user(db, email=ADMIN_EMAIL)
    await _assign(client, owner_token, event_a.id, ADMIN_EMAIL)
    await _assign(client, owner_token, event_b.id, ADMIN_EMAIL)

    await _remove(client, owner_token, event_a.id, admin.id)

    admins_a = await client.get(f"{API}/events/{event_a.id}/admins", headers=bearer(owner_token))
    assert admins_a.status_code == 200
    assert admins_a.json() == []

    admins_b = await client.get(f"{API}/events/{event_b.id}/admins", headers=bearer(owner_token))
    assert admins_b.status_code == 200
    assert [a["user_id"] for a in admins_b.json()] == [str(admin.id)]


# 29. Removal is audited -------------------------------------------------------------
async def test_removal_audited(client, app, db):
    owner, owner_token = await _setup_owner(client, app, db)
    event = await create_event(db, owner_user_id=owner.id)
    admin = await create_user(db, email=ADMIN_EMAIL)
    await _assign(client, owner_token, event.id, ADMIN_EMAIL)
    await _remove(client, owner_token, event.id, admin.id)

    entries = await _audit_entries(db, "event.admin_removed")
    assert len(entries) == 1
    assert entries[0].event_id == event.id
    assert entries[0].entity_id == admin.id
    assert entries[0].before_data == {
        "role": "EVENT_ADMIN",
        "permissions": EVENT_ADMIN_PERMISSIONS,
    }


# 30. Removed admin gets 403 with the same access token ------------------------------
async def test_removed_admin_denied_with_same_token(client, app, db):
    owner, owner_token = await _setup_owner(client, app, db)
    event = await create_event(db, owner_user_id=owner.id)
    admin = await create_user(db, email=ADMIN_EMAIL)
    access_token_for(app, admin)  # token issued before removal, still in the client's hands
    await _assign(client, owner_token, event.id, ADMIN_EMAIL)
    await _remove(client, owner_token, event.id, admin.id)

    # Same identity as the previously issued token, re-checked against PostgreSQL.
    with pytest.raises(ApiError) as exc:
        await deps.require_event_checkin_access(event_id=event.id, user=admin, db=db)
    assert exc.value.status_code == 403
    assert exc.value.code == "INSUFFICIENT_EVENT_PERMISSIONS"


# 31. Removed event disappears from /events/assigned ---------------------------------
async def test_removed_event_disappears_from_assigned(client, app, db):
    owner, owner_token = await _setup_owner(client, app, db)
    event = await create_event(db, owner_user_id=owner.id)
    admin = await create_user(db, email=ADMIN_EMAIL)
    admin_token = access_token_for(app, admin)
    await _assign(client, owner_token, event.id, ADMIN_EMAIL)

    assert (await client.get(f"{API}/events/assigned", headers=bearer(admin_token))).json()
    await _remove(client, owner_token, event.id, admin.id)

    resp = await client.get(f"{API}/events/assigned", headers=bearer(admin_token))
    assert resp.status_code == 200
    assert resp.json() == []


# 32. Removed admin loses check-in permission -----------------------------------------
async def test_removed_admin_loses_checkin_permission(client, app, db):
    owner, owner_token = await _setup_owner(client, app, db)
    event = await create_event(db, owner_user_id=owner.id)
    admin = await create_user(db, email=ADMIN_EMAIL)
    await _assign(client, owner_token, event.id, ADMIN_EMAIL)

    # Before removal the dependency grants check-in access.
    result = await deps.require_event_checkin_access(event_id=event.id, user=admin, db=db)
    assert result.id == event.id

    await _remove(client, owner_token, event.id, admin.id)

    with pytest.raises(ApiError) as exc:
        await deps.require_event_checkin_access(event_id=event.id, user=admin, db=db)
    assert exc.value.status_code == 403
    assert exc.value.code == "INSUFFICIENT_EVENT_PERMISSIONS"


# 33. Event staff endpoints require authentication ------------------------------------
async def test_event_staff_endpoints_require_auth(client, app, db):
    owner, _ = await _setup_owner(client, app, db)
    event = await create_event(db, owner_user_id=owner.id)
    admin = await create_user(db, email=ADMIN_EMAIL)

    assert (await client.get(f"{API}/events/assigned")).status_code == 401
    assert (await client.get(f"{API}/events/{event.id}/admins")).status_code == 401
    resp = await client.post(
        f"{API}/events/{event.id}/admins", json={"email": ADMIN_EMAIL}
    )
    assert resp.status_code == 401
    assert (
        await client.delete(f"{API}/events/{event.id}/admins/{admin.id}")
    ).status_code == 401

# 34. Owner sees owned events without an event_staff row ----------------------------
async def test_owner_sees_owned_events_without_staff_assignment(client, app, db):
    owner, owner_token = await _setup_owner(client, app, db)
    base = datetime.now(timezone.utc) + timedelta(days=1)
    first = await _create_event_at(
        db, owner_user_id=owner.id, title="Owner A", starts_at=base
    )
    second = await _create_event_at(
        db, owner_user_id=owner.id, title="Owner B", starts_at=base + timedelta(hours=1)
    )

    resp = await client.get(f"{API}/events/assigned", headers=bearer(owner_token))
    assert resp.status_code == 200
    body = resp.json()
    assert [item["event_id"] for item in body] == [str(first.id), str(second.id)]
    assert set(body[0]) == {
        "event_id",
        "title",
        "status",
        "visibility",
        "starts_at",
        "ends_at",
        "venue_name",
        "city",
        "timezone",
    }

    rows = (
        await db.execute(select(EventStaff).where(EventStaff.user_id == owner.id))
    ).scalars().all()
    assert rows == []


# 35. Event Admin sees all assigned events, in deterministic order -------------------
async def test_admin_sees_all_assigned_events(client, app, db):
    owner, owner_token = await _setup_owner(client, app, db)
    base = datetime.now(timezone.utc) + timedelta(days=1)
    first = await _create_event_at(
        db, owner_user_id=owner.id, title="First", starts_at=base
    )
    second = await _create_event_at(
        db, owner_user_id=owner.id, title="Second", starts_at=base + timedelta(hours=1)
    )
    third = await _create_event_at(
        db, owner_user_id=owner.id, title="Third", starts_at=base + timedelta(hours=2)
    )
    admin = await create_user(db, email=ADMIN_EMAIL)
    for event in (third, first, second):
        assert (await _assign(client, owner_token, event.id, ADMIN_EMAIL)).status_code == 201

    resp = await client.get(
        f"{API}/events/assigned", headers=bearer(access_token_for(app, admin))
    )
    assert resp.status_code == 200
    assert [item["event_id"] for item in resp.json()] == [
        str(first.id),
        str(second.id),
        str(third.id),
    ]


# 36. Check-In Staff sees assigned events when check-in policy allows them -----------
async def test_check_in_staff_sees_assigned_events(client, app, db):
    owner, _ = await _setup_owner(client, app, db)
    base = datetime.now(timezone.utc) + timedelta(days=1)
    assigned = await _create_event_at(
        db, owner_user_id=owner.id, title="Assigned", starts_at=base
    )
    await _create_event_at(
        db, owner_user_id=owner.id, title="Not Assigned", starts_at=base + timedelta(hours=1)
    )
    staff = await create_user(db, email="checkin-staff@example.com")
    await _assign_staff(
        db, event_id=assigned.id, user_id=staff.id, role=EventStaffRole.CHECK_IN_STAFF
    )

    resp = await client.get(
        f"{API}/events/assigned", headers=bearer(access_token_for(app, staff))
    )
    assert resp.status_code == 200
    assert [item["event_id"] for item in resp.json()] == [str(assigned.id)]

    allowed = await deps.require_event_checkin_access(
        event_id=assigned.id, user=staff, db=db
    )
    assert allowed.id == assigned.id


# 37. Owner of A and Event Admin of B sees both; /events still lists only owned ------
async def test_owner_and_staff_assignment_return_union(client, app, db):
    owner, owner_token = await _setup_owner(client, app, db)
    other_owner, other_token = await _setup_owner(
        client, app, db, email="owner2@example.com"
    )
    base = datetime.now(timezone.utc) + timedelta(days=1)
    owned = await _create_event_at(
        db, owner_user_id=owner.id, title="Owned", starts_at=base
    )
    assigned = await _create_event_at(
        db, owner_user_id=other_owner.id, title="Assigned", starts_at=base + timedelta(hours=1)
    )
    assert (await _assign(client, other_token, assigned.id, OWNER_EMAIL)).status_code == 201

    resp = await client.get(f"{API}/events/assigned", headers=bearer(owner_token))
    assert resp.status_code == 200
    assert [item["event_id"] for item in resp.json()] == [str(owned.id), str(assigned.id)]

    own_events = await client.get(f"{API}/events", headers=bearer(owner_token))
    assert own_events.status_code == 200
    assert [item["id"] for item in own_events.json()] == [str(owned.id)]


# 38. User with no ownership or assignments sees no events ---------------------------
async def test_unassigned_user_sees_no_events(client, app, db):
    owner, _ = await _setup_owner(client, app, db)
    await create_event(db, owner_user_id=owner.id)
    stranger = await create_user(db, email=STRANGER_EMAIL)

    resp = await client.get(
        f"{API}/events/assigned", headers=bearer(access_token_for(app, stranger))
    )
    assert resp.status_code == 200
    assert resp.json() == []


# 39. Removing a staff assignment leaves the owner's access intact -------------------
async def test_owner_access_persists_after_staff_assignment_removed(client, app, db):
    owner, owner_token = await _setup_owner(client, app, db)
    event = await create_event(db, owner_user_id=owner.id)
    admin = await create_user(db, email=ADMIN_EMAIL)
    admin_token = access_token_for(app, admin)

    assert (await _assign(client, owner_token, event.id, ADMIN_EMAIL)).status_code == 201
    assert (await _remove(client, owner_token, event.id, admin.id)).status_code == 204

    owner_resp = await client.get(f"{API}/events/assigned", headers=bearer(owner_token))
    assert [item["event_id"] for item in owner_resp.json()] == [str(event.id)]

    admin_resp = await client.get(f"{API}/events/assigned", headers=bearer(admin_token))
    assert admin_resp.json() == []


# 40. Overlapping ownership and staff membership does not duplicate an event ---------
async def test_owner_staff_overlap_returns_event_once(client, app, db):
    owner, owner_token = await _setup_owner(client, app, db)
    event = await create_event(db, owner_user_id=owner.id)
    await _assign_staff(
        db, event_id=event.id, user_id=owner.id, role=EventStaffRole.EVENT_ADMIN
    )

    resp = await client.get(f"{API}/events/assigned", headers=bearer(owner_token))
    assert resp.status_code == 200
    assert [item["event_id"] for item in resp.json()] == [str(event.id)]


# 41. Pagination and ordering operate on the combined authorization set --------------
async def test_assigned_pagination_operates_on_combined_set(client, app, db):
    owner, owner_token = await _setup_owner(client, app, db)
    other_owner, other_token = await _setup_owner(
        client, app, db, email="owner2@example.com"
    )
    base = datetime.now(timezone.utc) + timedelta(days=1)
    first = await _create_event_at(
        db, owner_user_id=owner.id, title="First", starts_at=base
    )
    second = await _create_event_at(
        db, owner_user_id=other_owner.id, title="Second", starts_at=base + timedelta(hours=1)
    )
    third = await _create_event_at(
        db, owner_user_id=owner.id, title="Third", starts_at=base + timedelta(hours=2)
    )
    fourth = await _create_event_at(
        db, owner_user_id=other_owner.id, title="Fourth", starts_at=base + timedelta(hours=3)
    )
    for event in (second, fourth):
        assert (await _assign(client, other_token, event.id, OWNER_EMAIL)).status_code == 201

    expected = [str(first.id), str(second.id), str(third.id), str(fourth.id)]
    full = await client.get(
        f"{API}/events/assigned?limit=100&offset=0", headers=bearer(owner_token)
    )
    repeated = await client.get(
        f"{API}/events/assigned?limit=100&offset=0", headers=bearer(owner_token)
    )
    assert [item["event_id"] for item in full.json()] == expected
    assert [item["event_id"] for item in repeated.json()] == expected

    page_one = await client.get(
        f"{API}/events/assigned?limit=2&offset=0", headers=bearer(owner_token)
    )
    page_two = await client.get(
        f"{API}/events/assigned?limit=2&offset=2", headers=bearer(owner_token)
    )
    paged = [item["event_id"] for item in page_one.json()] + [
        item["event_id"] for item in page_two.json()
    ]
    assert paged == expected
