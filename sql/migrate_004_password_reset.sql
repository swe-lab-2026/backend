-- BiletFlow auth: password reset tokens.
-- PostgreSQL 15+. Idempotent — safe to run on every startup.
-- Run AFTER migrate_002_auth.sql (depends on the `users` table).

BEGIN;

-- One row per password-reset request. Only the SHA-256 digest of the token is
-- stored; the raw token never touches PostgreSQL.
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_digest        TEXT NOT NULL,
    expires_at          TIMESTAMPTZ NOT NULL,
    used_at             TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_user
    ON password_reset_tokens (user_id);

COMMIT;
