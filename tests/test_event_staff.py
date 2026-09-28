import pytest
from sqlalchemy import select

from app.api import deps
from app.core.errors import ApiError
from app.db.models import AuditLog, UserRole, UserStatus
from tests.conftest import API, access_token_for, bearer, create_event, create_user

OWNER_EMAIL = "owner@example.com"
ADMIN_EMAIL = "admin@example.com"
STRANGER_EMAIL = "stranger@example.com"

EVENT_ADMIN_PERMISSIONS = {"view_event": True, "check_in_tickets": True}


async def _audit_entries(db, action: str):
    stmt = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id.asc())
    return (await db.execute(stmt)).scalars().all()


async def _setup_owner(client, app, db):
    owner = await create_user(db, email=OWNER_EMAIL)
    owner_token = access_token_for(app, owner)
    resp = await client.post(
        f"{API}/organizers/me",
        json={"display_name": "Owner Org", "terms_accepted": True},
        headers=bearer(owner_token),
    )
    assert resp.status_code == 201
    return owner, owner_token


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