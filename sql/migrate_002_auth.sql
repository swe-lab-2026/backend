-- BiletFlow auth: refresh sessions and email verification tokens.
-- PostgreSQL 15+. Idempotent — safe to run on every startup.
-- Run AFTER migrate_001.sql (depends on the `users` table).

BEGIN;

-- One row per issued refresh token. Only the SHA-256 digest of the token is
-- stored; the raw token never touches PostgreSQL.
CREATE TABLE IF NOT EXISTS refresh_sessions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash          TEXT NOT NULL,
    jti                 UUID NOT NULL UNIQUE,
    token_family        UUID NOT NULL,
    user_agent          VARCHAR(512),
    ip_address          VARCHAR(45),
    expires_at          TIMESTAMPTZ NOT NULL,
    revoked_at          TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at        TIMESTAMPTZ
);

-- One row per 6-digit email verification code. Only the SHA-256 digest of the
-- code is stored.
CREATE TABLE IF NOT EXISTS email_verification_tokens (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_digest         TEXT NOT NULL,
    expires_at          TIMESTAMPTZ NOT NULL,
    attempt_count       INTEGER NOT NULL DEFAULT 0,
    used_at             TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_refresh_sessions_user ON refresh_sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_refresh_sessions_jti ON refresh_sessions (jti);
-- Only live (unrevoked, unexpired) sessions, for "logout all active devices".
CREATE INDEX IF NOT EXISTS idx_refresh_sessions_active
    ON refresh_sessions (user_id, expires_at)
    WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_refresh_sessions_family ON refresh_sessions (token_family);
CREATE INDEX IF NOT EXISTS idx_email_verification_tokens_user
    ON email_verification_tokens (user_id);

COMMIT;