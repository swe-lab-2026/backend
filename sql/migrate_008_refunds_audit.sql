-- BiletFlow SCRUM-14: refund policy, idempotent simulated refunds, event
-- cancellation, notification outbox kinds and an append-only audit log.
-- PostgreSQL 15+. Run AFTER migrate_007_campaigns.sql. The runner wraps the
-- whole file in one transaction and records it exactly once.
-- Additive and backward compatible: new columns have safe defaults.

-- ---------- Event refund policy and cancellation ----------

-- Existing events default to "refunds disabled": nothing becomes refundable
-- without an explicit organizer decision.
ALTER TABLE events ADD COLUMN IF NOT EXISTS refunds_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE events ADD COLUMN IF NOT EXISTS refund_deadline_hours INTEGER;
ALTER TABLE events ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ;
ALTER TABLE events ADD COLUMN IF NOT EXISTS cancellation_reason TEXT;
ALTER TABLE events
    ADD CONSTRAINT events_refund_deadline_valid
    CHECK (refund_deadline_hours IS NULL OR refund_deadline_hours BETWEEN 0 AND 8760);

-- ---------- Refunds ----------

ALTER TABLE refunds ADD COLUMN IF NOT EXISTS kind VARCHAR(30) NOT NULL DEFAULT 'CUSTOMER_REQUEST';
ALTER TABLE refunds ADD COLUMN IF NOT EXISTS currency CHAR(3) NOT NULL DEFAULT 'KZT';
ALTER TABLE refunds ADD COLUMN IF NOT EXISTS is_simulated BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE refunds ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
ALTER TABLE refunds
    ADD CONSTRAINT refunds_kind_valid
    CHECK (kind IN ('CUSTOMER_REQUEST', 'EVENT_CANCELLATION'));

-- Full refunds only: one non-failed refund per payment. Repeated or concurrent
-- refund requests can never create a second payout.
CREATE UNIQUE INDEX IF NOT EXISTS uq_refunds_active_per_payment
    ON refunds (payment_id)
    WHERE status IN ('PENDING', 'SUCCEEDED');

-- ---------- Notification outbox ----------

ALTER TABLE order_notifications ADD COLUMN IF NOT EXISTS dedupe_key VARCHAR(100) NOT NULL DEFAULT '';
ALTER TABLE order_notifications ADD COLUMN IF NOT EXISTS payload JSONB;
ALTER TABLE order_notifications DROP CONSTRAINT IF EXISTS uq_order_notifications_order_kind;
ALTER TABLE order_notifications
    ADD CONSTRAINT uq_order_notifications_order_kind_key UNIQUE (order_id, kind, dedupe_key);

-- ---------- Audit log ----------

ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS actor_type VARCHAR(20) NOT NULL DEFAULT 'SYSTEM';
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS metadata JSONB;
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS request_id VARCHAR(64);

-- Backfill before the append-only trigger exists.
UPDATE audit_logs SET actor_type = 'USER' WHERE actor_user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_audit_logs_event
    ON audit_logs (event_id, created_at DESC)
    WHERE event_id IS NOT NULL;

-- Append-only enforcement. Rows can be inserted, never edited or deleted.
-- The only permitted UPDATE is the FK cascade that nulls actor_user_id /
-- event_id when a referenced user or draft event is deleted. This protects
-- against application bugs and ordinary SQL access; a database superuser or
-- the table owner could still disable the trigger, so this is not a
-- cryptographic immutability guarantee.
CREATE OR REPLACE FUNCTION audit_logs_append_only()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'UPDATE'
        AND NEW.id = OLD.id
        AND NEW.entity_type = OLD.entity_type
        AND NEW.entity_id IS NOT DISTINCT FROM OLD.entity_id
        AND NEW.action = OLD.action
        AND NEW.actor_type = OLD.actor_type
        AND NEW.before_data IS NOT DISTINCT FROM OLD.before_data
        AND NEW.after_data IS NOT DISTINCT FROM OLD.after_data
        AND NEW.metadata IS NOT DISTINCT FROM OLD.metadata
        AND NEW.request_id IS NOT DISTINCT FROM OLD.request_id
        AND NEW.ip_address IS NOT DISTINCT FROM OLD.ip_address
        AND NEW.created_at = OLD.created_at
        AND (NEW.actor_user_id IS NOT DISTINCT FROM OLD.actor_user_id OR NEW.actor_user_id IS NULL)
        AND (NEW.event_id IS NOT DISTINCT FROM OLD.event_id OR NEW.event_id IS NULL)
    THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'audit_logs is append-only (% rejected)', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

DROP TRIGGER IF EXISTS trg_audit_logs_append_only ON audit_logs;
CREATE TRIGGER trg_audit_logs_append_only
    BEFORE UPDATE OR DELETE ON audit_logs
    FOR EACH ROW EXECUTE FUNCTION audit_logs_append_only();

DROP TRIGGER IF EXISTS trg_audit_logs_no_truncate ON audit_logs;
CREATE TRIGGER trg_audit_logs_no_truncate
    BEFORE TRUNCATE ON audit_logs
    FOR EACH STATEMENT EXECUTE FUNCTION audit_logs_append_only();
