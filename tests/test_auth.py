from datetime import datetime, timedelta, timezone

import jwt
from sqlalchemy import select

from app.core.security import verify_password
from app.db.models import EmailVerificationToken, UserRole, UserStatus
from tests.conftest import API, bearer, get_code, get_user, set_user_status

EMAIL = "user@example.com"
PASSWORD = "StrongPassword123!"


async def _register(client, *, email=EMAIL, password=PASSWORD, **overrides):
    payload = {
        "email": email,
        "password": password,
        "first_name": "Amin",
        "last_name": "Zhubanov",
        **overrides,
    }
    return await client.post(f"{API}/auth/register", json=payload)


async def _verify(client, app, *, email=EMAIL):
    code = await get_code(app, email)
    return await client.post(f"{API}/auth/verify-email", json={"email": email, "code": code})


async def _login(client, *, email=EMAIL, password=PASSWORD):
    return await client.post(f"{API}/auth/login", json={"email": email, "password": password})


async def _full_user(client, app, *, email=EMAIL):
    """Register, verify and log in; returns the login token body."""
    assert (await _register(client, email=email)).status_code == 201
    assert (await _verify(client, app, email=email)).status_code == 200
    resp = await _login(client, email=email)
    assert resp.status_code == 200
    return resp.json()


async def _refresh(client, refresh_token: str):
    return await client.post(f"{API}/auth/refresh", json={"refresh_token": refresh_token})


# 1. Successful registration -------------------------------------------------
async def test_register_success(client, db):
    resp = await _register(client)
    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == EMAIL
    assert body["email_verified"] is False
    assert "access_token" not in body
    assert "refresh_token" not in body
    assert "code" not in body

    user = await get_user(db, EMAIL)
    assert user is not None
    assert user.email_verified_at is None
    assert user.global_role == UserRole.USER
    assert user.status == UserStatus.ACTIVE


# 2. Duplicate email ---------------------------------------------------------
async def test_register_duplicate_email(client):
    assert (await _register(client)).status_code == 201
    resp = await _register(client)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "EMAIL_ALREADY_EXISTS"


# 3. Role cannot be chosen via body ------------------------------------------
async def test_register_cannot_choose_role(client, db):
    resp = await _register(client, global_role="PLATFORM_ADMIN")
    assert resp.status_code == 201
    user = await get_user(db, EMAIL)
    assert user is not None
    assert user.global_role == UserRole.USER


# 4. Password stored only as a hash ------------------------------------------
async def test_password_stored_as_hash(client, db):
    assert (await _register(client)).status_code == 201
    user = await get_user(db, EMAIL)
    assert user is not None
    assert user.password_hash != PASSWORD
    assert user.password_hash.startswith("$2")
    assert verify_password(PASSWORD, user.password_hash)
    assert not verify_password("WrongPassword123!", user.password_hash)


# 5. Successful email verification -------------------------------------------
async def test_verify_email_success(client, app, db):
    assert (await _register(client)).status_code == 201
    resp = await _verify(client, app)
    assert resp.status_code == 200
    assert resp.json()["email_verified"] is True
    user = await get_user(db, EMAIL)
    assert user is not None
    assert user.email_verified_at is not None


# 6. Wrong verification code -------------------------------------------------
async def test_verify_wrong_code(client, app):
    assert (await _register(client)).status_code == 201
    code = await get_code(app, EMAIL)
    wrong = f"{(int(code) + 1) % 1_000_000:06d}"
    resp = await client.post(
        f"{API}/auth/verify-email", json={"email": EMAIL, "code": wrong}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_VERIFICATION_CODE"


# 7. Expired verification code ------------------------------------------------
async def test_verify_expired_code(client, app, db):
    assert (await _register(client)).status_code == 201
    token = (
        await db.execute(
            select(EmailVerificationToken).order_by(EmailVerificationToken.created_at.desc()).limit(1)
        )
    ).scalar_one()
    token.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db.commit()

    resp = await _verify(client, app)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VERIFICATION_CODE_EXPIRED"


# 8. Code cannot be reused ----------------------------------------------------
async def test_verify_code_reuse(client, app):
    assert (await _register(client)).status_code == 201
    assert (await _verify(client, app)).status_code == 200
    resp = await _verify(client, app)  # same code, already used
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_VERIFICATION_CODE"


# 9. Login before email verification is denied --------------------------------
async def test_login_before_verification(client):
    assert (await _register(client)).status_code == 201
    resp = await _login(client)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "EMAIL_NOT_VERIFIED"


# 10. Login with correct password ---------------------------------------------
async def test_login_success(client, app):
    body = await _full_user(client, app)
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0


# 11. Login with wrong password -----------------------------------------------
async def test_login_wrong_password(client, app):
    await _full_user(client, app)
    resp = await _login(client, password="WrongPassword123!")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "INVALID_CREDENTIALS"


# 11b. Unknown email returns the same error (no enumeration) -------------------
async def test_login_unknown_email(client):
    resp = await _login(client, email="nobody@example.com")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "INVALID_CREDENTIALS"


# 12. Login of a blocked user -------------------------------------------------
async def test_login_blocked_user(client, app, db):
    body = await _full_user(client, app)
    user = await get_user(db, EMAIL)
    await set_user_status(db, user.id, UserStatus.BLOCKED)
    resp = await _login(client)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "USER_BLOCKED"
    # Previously issued access token must also stop working.
    me = await client.get(f"{API}/users/me", headers=bearer(body["access_token"]))
    assert me.status_code == 403
    assert me.json()["error"]["code"] == "USER_BLOCKED"


# 13. Access token opens a protected endpoint ----------------------------------
async def test_access_token_opens_protected_endpoint(client, app):
    body = await _full_user(client, app)
    resp = await client.get(f"{API}/users/me", headers=bearer(body["access_token"]))
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == EMAIL
    assert data["global_role"] == "USER"
    assert "password_hash" not in data


# 14. Refresh token cannot be used as an access token --------------------------
async def test_refresh_token_cannot_be_access_token(client, app):
    body = await _full_user(client, app)
    resp = await client.get(f"{API}/users/me", headers=bearer(body["refresh_token"]))
    assert resp.status_code == 401


# 15. Access token cannot be used for refresh ----------------------------------
async def test_access_token_cannot_be_used_for_refresh(client, app):
    body = await _full_user(client, app)
    resp = await _refresh(client, body["access_token"])
    assert resp.status_code == 401


# 16. Expired token is rejected ------------------------------------------------
async def test_expired_token_rejected(client, app, db):
    await _full_user(client, app)
    user = await get_user(db, EMAIL)
    settings = app.state.settings
    now = datetime.now(timezone.utc)
    expired = jwt.encode(
        {
            "sub": str(user.id),
            "role": "USER",
            "token_type": "access",
            "jti": "expired-test",
            "iat": now - timedelta(hours=2),
            "exp": now - timedelta(hours=1),
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
        },
        settings.jwt_access_secret,
        algorithm=settings.jwt_algorithm,
    )
    resp = await client.get(f"{API}/users/me", headers=bearer(expired))
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "TOKEN_EXPIRED"


# 17. Refresh rotation issues a new pair ----------------------------------------
async def test_refresh_rotation_issues_new_pair(client, app):
    body = await _full_user(client, app)
    resp = await _refresh(client, body["refresh_token"])
    assert resp.status_code == 200
    new = resp.json()
    assert new["access_token"]
    assert new["refresh_token"]
    assert new["refresh_token"] != body["refresh_token"]
    assert new["access_token"] != body["access_token"]


# 18. Reusing a revoked refresh token revokes the family -------------------------
async def test_refresh_reuse_revokes_family(client, app):
    body = await _full_user(client, app)
    first = await _refresh(client, body["refresh_token"])
    assert first.status_code == 200
    new = first.json()

    replay = await _refresh(client, body["refresh_token"])
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "REFRESH_TOKEN_REUSED"

    # The rotated token belongs to the same family and is now revoked too.
    rotated = await _refresh(client, new["refresh_token"])
    assert rotated.status_code == 401


# 19. Logout revokes the current session ----------------------------------------
async def test_logout_revokes_session(client, app):
    body = await _full_user(client, app)
    resp = await client.post(f"{API}/auth/logout", json={"refresh_token": body["refresh_token"]})
    assert resp.status_code == 200
    after = await _refresh(client, body["refresh_token"])
    assert after.status_code == 401


# 20. Logout-all revokes every session --------------------------------------------
async def test_logout_all_revokes_all_sessions(client, app):
    body1 = await _full_user(client, app)
    second_login = await _login(client)
    assert second_login.status_code == 200
    body2 = second_login.json()
    assert body2["refresh_token"] != body1["refresh_token"]

    resp = await client.post(
        f"{API}/auth/logout-all", headers=bearer(body1["access_token"])
    )
    assert resp.status_code == 200

    for token in (body1["refresh_token"], body2["refresh_token"]):
        after = await _refresh(client, token)
        assert after.status_code == 401


# 21. USER role gets 403 on an admin endpoint -------------------------------------
async def test_user_role_forbidden_on_admin(client, app):
    body = await _full_user(client, app)
    resp = await client.get(f"{API}/admin/example", headers=bearer(body["access_token"]))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "INSUFFICIENT_PERMISSIONS"


# 22. PLATFORM_ADMIN passes the role check ----------------------------------------
async def test_platform_admin_passes_role_check(client, app, db):
    body = await _full_user(client, app)
    user = await get_user(db, EMAIL)
    user.global_role = UserRole.PLATFORM_ADMIN
    await db.commit()

    resp = await client.get(f"{API}/admin/example", headers=bearer(body["access_token"]))
    assert resp.status_code == 200
    assert resp.json()["user_id"] == str(user.id)


# 23. No token -> 401 --------------------------------------------------------------
async def test_no_token_gets_401(client):
    resp = await client.get(f"{API}/users/me")
    assert resp.status_code == 401


# 24. Blocked after token issuance loses access -------------------------------------
async def test_blocked_user_loses_access(client, app, db):
    body = await _full_user(client, app)
    user = await get_user(db, EMAIL)
    await set_user_status(db, user.id, UserStatus.BLOCKED)

    resp = await client.get(f"{API}/users/me", headers=bearer(body["access_token"]))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "USER_BLOCKED"


# 25. Login rate limiting ------------------------------------------------------------
async def test_login_rate_limit(client, app):
    assert (await _register(client)).status_code == 201
    assert (await _verify(client, app)).status_code == 200
    for _ in range(5):
        resp = await _login(client, password="WrongPassword123!")
        assert resp.status_code == 401
    resp = await _login(client, password="WrongPassword123!")
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "TOO_MANY_ATTEMPTS"