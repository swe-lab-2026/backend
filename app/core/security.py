import base64
import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from secrets import randbelow

import bcrypt
import jwt
from jwt import ExpiredSignatureError, InvalidTokenError

from app.core.config import Settings
from app.core.errors import ApiError

TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"

# bcrypt over base64(sha256(password)): keeps full Unicode support and removes
# bcrypt's silent 72-byte truncation while staying a plain bcrypt hash.
_BCRYPT_ROUNDS = 12

# Verified against when a login reaches an unknown email so that the "user not
# found" path takes the same time as a real password check (timing-safe user
# enumeration defence).
_DUMMY_PASSWORD_HASH = bcrypt.hashpw(
    base64.b64encode(hashlib.sha256(b"timing-equalization-dummy").digest()),
    bcrypt.gensalt(rounds=_BCRYPT_ROUNDS),
).decode("ascii")


@dataclass(frozen=True)
class TokenClaims:
    user_id: str
    role: str
    token_type: str
    jti: str
    exp: datetime


def _secret_for(settings: Settings, token_type: str) -> str:
    if token_type == TOKEN_TYPE_REFRESH:
        return settings.jwt_refresh_secret
    return settings.jwt_access_secret


def _base_claims(settings: Settings, user_id: str, role: str, token_type: str) -> dict[str, object]:
    return {
        "sub": user_id,
        "role": role,
        "token_type": token_type,
        "jti": uuid.uuid4().hex,
        "iat": datetime.now(timezone.utc),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
    }


def create_access_token(settings: Settings, *, user_id: str, role: str) -> str:
    claims = _base_claims(settings, user_id, role, TOKEN_TYPE_ACCESS)
    ttl = timedelta(minutes=settings.access_token_ttl_minutes)
    claims["exp"] = datetime.now(timezone.utc) + ttl
    return jwt.encode(claims, settings.jwt_access_secret, algorithm=settings.jwt_algorithm)


def create_refresh_token(settings: Settings, *, user_id: str, role: str, jti: str) -> str:
    claims = _base_claims(settings, user_id, role, TOKEN_TYPE_REFRESH)
    claims["jti"] = jti
    claims["exp"] = datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_ttl_days)
    return jwt.encode(claims, settings.jwt_refresh_secret, algorithm=settings.jwt_algorithm)


def decode_token(settings: Settings, token: str, expected_type: str) -> TokenClaims:
    """Validate signature, exp, iss, aud, token_type and jti.

    Only the pinned algorithm from settings is accepted; `none` is never
    allowed (PyJWT refuses algorithms outside the explicit list).
    """
    try:
        payload = jwt.decode(
            token,
            _secret_for(settings, expected_type),
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"require": ["sub", "exp", "iat", "token_type", "jti"]},
        )
    except ExpiredSignatureError as exc:
        raise ApiError(401, "TOKEN_EXPIRED", "Token has expired") from exc
    except InvalidTokenError as exc:
        raise ApiError(401, "TOKEN_INVALID", "Token is invalid") from exc

    user_id = payload.get("sub")
    token_type = payload.get("token_type")
    jti = payload.get("jti")
    role = payload.get("role")
    if not isinstance(user_id, str) or not user_id:
        raise ApiError(401, "TOKEN_INVALID", "Token has no subject")
    if token_type != expected_type:
        raise ApiError(401, "TOKEN_INVALID", f"Expected a {expected_type} token")
    if not isinstance(jti, str) or not jti:
        raise ApiError(401, "TOKEN_INVALID", "Token has no id")

    return TokenClaims(
        user_id=user_id,
        role=role if isinstance(role, str) else "",
        token_type=token_type,
        jti=jti,
        exp=datetime.fromtimestamp(int(payload["exp"]), tz=timezone.utc),
    )


# ---------- Password hashing ----------

def hash_password(password: str) -> str:
    prehash = base64.b64encode(hashlib.sha256(password.encode("utf-8")).digest())
    return bcrypt.hashpw(prehash, bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    prehash = base64.b64encode(hashlib.sha256(password.encode("utf-8")).digest())
    try:
        return bcrypt.checkpw(prehash, password_hash.encode("ascii"))
    except ValueError:
        return False


def dummy_verify() -> None:
    """Burn a bcrypt verification against a dummy hash (timing equalization)."""
    verify_password("dummy-timing", _DUMMY_PASSWORD_HASH)


# ---------- Digests ----------

def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def constant_time_eq(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)


def new_verification_code() -> str:
    return f"{randbelow(1_000_000):06d}"