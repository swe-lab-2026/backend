-- BiletFlow admission tickets: free-registration order status and the
-- transactional email outbox used after order confirmation.
-- PostgreSQL 15+. Run AFTER migrate_004_password_reset.sql.

BEGIN;

-- Free registrations are confirmed without a payment step. PAID remains the
-- committed state for paid checkouts.
ALTER TYPE order_status ADD VALUE IF NOT EXISTS 'CONFIRMED';

-- One row per order confirmation email. The unique constraint makes enqueueing
-- idempotent when confirmation is retried.
CREATE TABLE IF NOT EXISTS order_notifications (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id    UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    kind        VARCHAR(50) NOT NULL DEFAULT 'ORDER_CONFIRMATION',
    status      VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    sent_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT order_notifications_status_valid
        CHECK (status IN ('PENDING', 'SENT', 'FAILED')),
    CONSTRAINT uq_order_notifications_order_kind UNIQUE (order_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_order_notifications_pending
    ON order_notifications (status, created_at)
    WHERE status IN ('PENDING', 'FAILED');

COMMIT;
