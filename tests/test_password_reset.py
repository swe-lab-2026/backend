from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.security import sha256_hex
from app.db.models import AuditLog, PasswordResetToken
from tests.conftest import API

EMAIL = "reset-user@example.com"
PASSWORD = "StrongPassword123!"
NEW_PASSWORD = "BrandNewPassword456!"


async def _register(client, *, email=EMAIL, password=PASSWORD):
    return await client.post(
        f"{API}/auth/register",
        json={
            "email": email,
            "password": password,
            "first_name": "Reset",
            "last_name": "User",
        },
    )


async def _request_reset(client, *, email=EMAIL):
    return await client.post(f"{API}/auth/request-password-reset", json={"email": email})


async def _confirm_reset(client, *, email=EMAIL, token, new_password=NEW_PASSWORD):
    return await client.post(
        f"{API}/auth/reset-password",
        json={"email": email, "token": token, "new_password": new_password},
    )


async def _reset_token(app, email: str) -> str:
    token = app.state.email_provider.last_reset_token_for(email)
    assert token is not None, f"no reset token was sent to {email}"
    return token


async def _token_row(db, email: str):
    from tests.conftest import get_user

    user = await get_user(db, email)
    assert user is not None
    row = (
        await db.execute(
            select(PasswordResetToken)
            .where(PasswordResetToken.user_id == user.id)
            .order_by(PasswordResetToken.created_at.desc())
            .limit(1)
        )
    ).scalar_one()
    # The shared `db` session keeps an identity map across API requests (which
    # commit in their own sessions), so refresh before asserting on fresh state.
    await db.refresh(row)
    return row


# 1. Successful reset end to end ----------------------------------------------
async def test_password_reset_success(client, app, db):
    assert (await _register(client)).status_code == 201
    verify = app.state.email_provider.last_code_for(EMAIL)
    assert verify is not None
    assert (
        await client.post(f"{API}/auth/verify-email", json={"email": EMAIL, "code": verify})
    ).status_code == 200

    resp = await _request_reset(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == EMAIL
    assert "token" not in body

    token = await _reset_token(app, EMAIL)
    row = await _token_row(db, EMAIL)
    assert row.used_at is None
    # Only the digest is stored, never the raw token.
    assert row.token_digest == sha256_hex(token)
    assert token not in row.token_digest

    confirm = await _confirm_reset(client, token=token)
    assert confirm.status_code == 200
    assert confirm.json()["email"] == EMAIL
    assert "token" not in confirm.json()

    row = await _token_row(db, EMAIL)
    assert row.used_at is not None

    from tests.conftest import get_user

    user = await get_user(db, EMAIL)
    assert user is not None
    assert user.password_hash.startswith("$2")
    assert user.password_hash != PASSWORD

    # New password works, old one does not.
    assert (
        await client.post(f"{API}/auth/login", json={"email": EMAIL, "password": NEW_PASSWORD})
    ).status_code == 200
    bad = await client.post(f"{API}/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert bad.status_code == 401

    entries = (
        await db.execute(select(AuditLog).where(AuditLog.action == "auth.password_reset"))
    ).scalars().all()
    assert len(entries) == 1


# 2. Unknown email is indistinguishable from a real one -------------------------
async def test_password_reset_unknown_email(client, app):
    known_shape = None
    assert (await _register(client)).status_code == 201
    known = await _request_reset(client)
    assert known.status_code == 200
    known_shape = set(known.json().keys())

    resp = await _request_reset(client, email="nobody@example.com")
    assert resp.status_code == 200
    assert set(resp.json().keys()) == known_shape
    assert "token" not in resp.json()
    assert app.state.email_provider.last_reset_token_for("nobody@example.com") is None


# 3. Invalid token is rejected --------------------------------------------------
async def test_password_reset_invalid_token(client, app):
    assert (await _register(client)).status_code == 201
    assert (await _request_reset(client)).status_code == 200

    resp = await _confirm_reset(client, token="0" * 43)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_RESET_TOKEN"


# 4. Expired token is rejected ---------------------------------------------------
async def test_password_reset_expired_token(client, app, db):
    assert (await _register(client)).status_code == 201
    assert (await _request_reset(client)).status_code == 200
    token = await _reset_token(app, EMAIL)

    row = await _token_row(db, EMAIL)
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db.commit()

    resp = await _confirm_reset(client, token=token)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "RESET_TOKEN_EXPIRED"


# 5. A token cannot be reused -----------------------------------------------------
async def test_password_reset_token_reuse(client, app):
    assert (await _register(client)).status_code == 201
    assert (await _request_reset(client)).status_code == 200
    token = await _reset_token(app, EMAIL)

    assert (await _confirm_reset(client, token=token)).status_code == 200
    reuse = await _confirm_reset(client, token=token)
    assert reuse.status_code == 400
    assert reuse.json()["error"]["code"] == "INVALID_RESET_TOKEN"


# 6. A newer request invalidates the previous token --------------------------------
async def test_password_reset_latest_token_wins(client, app):
    assert (await _register(client)).status_code == 201
    assert (await _request_reset(client)).status_code == 200
    first = await _reset_token(app, EMAIL)

    await app.state.store.delete(f"pwdreset:{EMAIL}")
    assert (await _request_reset(client)).status_code == 200
    second = await _reset_token(app, EMAIL)
    assert second != first

    stale = await _confirm_reset(client, token=first)
    assert stale.status_code == 400
    assert stale.json()["error"]["code"] == "INVALID_RESET_TOKEN"

    assert (await _confirm_reset(client, token=second)).status_code == 200


# 7. Weak replacement passwords are rejected ---------------------------------------
async def test_password_reset_weak_password(client, app):
    assert (await _register(client)).status_code == 201
    assert (await _request_reset(client)).status_code == 200
    token = await _reset_token(app, EMAIL)

    resp = await _confirm_reset(client, token=token, new_password="weak")
    assert resp.status_code == 422


# 8. Reset kills sessions minted under the old password ----------------------------
async def test_password_reset_revokes_sessions(client, app):
    assert (await _register(client)).status_code == 201
    verify = app.state.email_provider.last_code_for(EMAIL)
    assert verify is not None
    assert (
        await client.post(f"{API}/auth/verify-email", json={"email": EMAIL, "code": verify})
    ).status_code == 200
    login = await client.post(
        f"{API}/auth/login", json={"email": EMAIL, "password": PASSWORD}
    )
    assert login.status_code == 200
    refresh_token = login.json()["refresh_token"]

    assert (await _request_reset(client)).status_code == 200
    assert (await _confirm_reset(client, token=await _reset_token(app, EMAIL))).status_code == 200

    stale = await client.post(f"{API}/auth/refresh", json={"refresh_token": refresh_token})
    assert stale.status_code == 401


# 9. Immediate re-request is throttled ----------------------------------------------
async def test_password_reset_request_cooldown(client):
    assert (await _register(client)).status_code == 201
    assert (await _request_reset(client)).status_code == 200

    second = await _request_reset(client)
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "TOO_MANY_ATTEMPTS"
