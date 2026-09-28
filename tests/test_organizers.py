from sqlalchemy import select

from app.db.models import AuditLog, PayoutAccount
from tests.conftest import API, access_token_for, bearer, create_user

OWNER_EMAIL = "owner@example.com"


async def _create_profile(client, token, **overrides):
    payload = {"display_name": "BiletFlow Events", "terms_accepted": True, **overrides}
    return await client.post(f"{API}/organizers/me", json=payload, headers=bearer(token))


async def _audit_actions(db, action: str):
    stmt = (
        select(AuditLog)
        .where(AuditLog.action == action)
        .order_by(AuditLog.id.asc())
    )
    return (await db.execute(stmt)).scalars().all()


async def _make_owner(client, app, db, *, email=OWNER_EMAIL, verified=True):
    user = await create_user(db, email=email, verified=verified)
    return user, access_token_for(app, user)


# 1. No JWT -> 401 ------------------------------------------------------------
async def test_create_profile_requires_auth(client):
    resp = await client.post(
        f"{API}/organizers/me",
        json={"display_name": "X", "terms_accepted": True},
    )
    assert resp.status_code == 401


# 2. Unverified email -> 403 ---------------------------------------------------
async def test_create_profile_unverified_email(client, app, db):
    _, token = await _make_owner(client, app, db, verified=False)
    resp = await _create_profile(client, token)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "EMAIL_NOT_VERIFIED"


# 3. Verified user creates own profile -----------------------------------------
async def test_create_profile_success(client, app, db):
    user, token = await _make_owner(client, app, db)
    resp = await _create_profile(client, token)
    assert resp.status_code == 201
    body = resp.json()
    assert body["user_id"] == str(user.id)
    assert body["display_name"] == "BiletFlow Events"
    assert body["verification_status"] == "NOT_SUBMITTED"
    assert body["terms_accepted_at"] is not None


# 4. user_id from body is ignored ----------------------------------------------
async def test_create_profile_ignores_user_id(client, app, db):
    user, token = await _make_owner(client, app, db)
    other = await create_user(db, email="other@example.com")
    resp = await _create_profile(client, token, user_id=str(other.id))
    assert resp.status_code == 201
    assert resp.json()["user_id"] == str(user.id)


# 5. Duplicate create -> 409 ---------------------------------------------------
async def test_create_profile_duplicate(client, app, db):
    _, token = await _make_owner(client, app, db)
    assert (await _create_profile(client, token)).status_code == 201
    resp = await _create_profile(client, token)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "ORGANIZER_PROFILE_ALREADY_EXISTS"


# 6. User reads only own profile -----------------------------------------------
async def test_get_profile_own_only(client, app, db):
    owner, owner_token = await _make_owner(client, app, db)
    await _create_profile(client, owner_token)
    resp = await client.get(f"{API}/organizers/me", headers=bearer(owner_token))
    assert resp.status_code == 200
    assert resp.json()["user_id"] == str(owner.id)

    stranger = await create_user(db, email="stranger@example.com")
    resp = await client.get(
        f"{API}/organizers/me", headers=bearer(access_token_for(app, stranger))
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "ORGANIZER_PROFILE_NOT_FOUND"


# 7. User updates own profile --------------------------------------------------
async def test_update_profile(client, app, db):
    _, token = await _make_owner(client, app, db)
    await _create_profile(client, token, legal_name="BiletFlow Events LLP")
    resp = await client.patch(
        f"{API}/organizers/me",
        json={"display_name": "Renamed Events"},
        headers=bearer(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["display_name"] == "Renamed Events"
    # Fields absent from the request are preserved.
    assert body["legal_name"] == "BiletFlow Events LLP"


# 8. verification_status cannot be changed via body ----------------------------
async def test_update_profile_cannot_change_verification_status(client, app, db):
    _, token = await _make_owner(client, app, db)
    await _create_profile(client, token)
    resp = await client.patch(
        f"{API}/organizers/me",
        json={"verification_status": "VERIFIED"},
        headers=bearer(token),
    )
    assert resp.status_code == 200
    assert resp.json()["verification_status"] == "NOT_SUBMITTED"


# 9. Profile creation is audited -----------------------------------------------
async def test_profile_created_audited(client, app, db):
    _, token = await _make_owner(client, app, db)
    await _create_profile(client, token)
    entries = await _audit_actions(db, "organizer.profile_created")
    assert len(entries) == 1


# 10. Profile update is audited -------------------------------------------------
async def test_profile_updated_audited(client, app, db):
    _, token = await _make_owner(client, app, db)
    await _create_profile(client, token)
    await client.patch(f"{API}/organizers/me", json={"display_name": "New"}, headers=bearer(token))
    entries = await _audit_actions(db, "organizer.profile_updated")
    assert len(entries) == 1
    assert entries[0].after_data == {"display_name": "New"}


# 11. Default payout account is created -----------------------------------------
async def test_payout_account_created(client, app, db):
    _, token = await _make_owner(client, app, db)
    await _create_profile(client, token)
    resp = await client.put(
        f"{API}/organizers/me/payout-account",
        json={"provider": "DEMO", "external_account_ref": "demo-account-001", "is_default": True},
        headers=bearer(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "PENDING"
    assert body["is_default"] is True
    assert body["external_account_ref"] == "****-001"  # masked, not the raw reference


# 12. New default replaces the previous default ---------------------------------
async def test_new_default_replaces_previous(client, app, db):
    _, token = await _make_owner(client, app, db)
    await _create_profile(client, token)
    first = await client.put(
        f"{API}/organizers/me/payout-account",
        json={"provider": "DEMO", "external_account_ref": "demo-account-001", "is_default": True},
        headers=bearer(token),
    )
    assert first.status_code == 200
    second = await client.put(
        f"{API}/organizers/me/payout-account",
        json={"provider": "DEMO", "external_account_ref": "demo-account-002", "is_default": True},
        headers=bearer(token),
    )
    assert second.status_code == 200
    assert second.json()["is_default"] is True

    accounts = (
        await db.execute(
            select(PayoutAccount).order_by(PayoutAccount.created_at.asc())
        )
    ).scalars().all()
    assert len(accounts) == 2
    assert accounts[0].is_default is False
    assert accounts[1].is_default is True


# 13. Payout change is audited --------------------------------------------------
async def test_payout_account_audited(client, app, db):
    _, token = await _make_owner(client, app, db)
    await _create_profile(client, token)
    await client.put(
        f"{API}/organizers/me/payout-account",
        json={"provider": "DEMO", "external_account_ref": "demo-account-001", "is_default": True},
        headers=bearer(token),
    )
    entries = await _audit_actions(db, "organizer.payout_account_updated")
    assert len(entries) == 1
    assert entries[0].after_data == {"provider": "DEMO", "is_default": True}