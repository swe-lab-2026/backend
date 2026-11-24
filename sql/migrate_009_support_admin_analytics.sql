-- BiletFlow SCRUM-16 (support cases and asynchronous messages) and SCRUM-17
-- (administration, suspension, monitoring, analytics and event history).
-- PostgreSQL 15+. Run AFTER migrate_008_refunds_audit.sql. The runner wraps the
-- whole file in one transaction and records it exactly once.
--
-- Additive and backward compatible: every new column is nullable or has a
-- default, existing rows are backfilled, nothing is dropped. Support messages,
-- financial records and audit history are preserved.

-- ---------- Support cases (tables exist since migrate_001) ----------

-- New lifecycle state. ADD VALUE is allowed inside a transaction on PG 12+;
-- the value is deliberately not referenced anywhere else in this file because
-- a freshly added enum value cannot be used before the transaction commits.
ALTER TYPE support_case_status ADD VALUE IF NOT EXISTS 'WAITING_FOR_CUSTOMER';

-- Routing: ATTENDEE_ORGANIZER (customer <-> event organizer staff) or
-- PLATFORM (organizer/user <-> Platform Admins).
ALTER TABLE support_cases
    ADD COLUMN IF NOT EXISTS case_type VARCHAR(30) NOT NULL DEFAULT 'PLATFORM';
ALTER TABLE support_cases ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ;
ALTER TABLE support_cases ADD COLUMN IF NOT EXISTS escalated_at TIMESTAMPTZ;
ALTER TABLE support_cases
    ADD COLUMN IF NOT EXISTS escalated_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE support_cases ADD COLUMN IF NOT EXISTS escalation_reason TEXT;

-- Legacy rows (none are created by earlier code): a case about someone else's
-- event is an attendee case; everything else is platform support.
UPDATE support_cases c
SET case_type = 'ATTENDEE_ORGANIZER'
WHERE c.event_id IS NOT NULL
  AND c.requester_user_id IS DISTINCT FROM (
      SELECT e.owner_user_id FROM events e WHERE e.id = c.event_id
  );
UPDATE support_cases SET resolved_at = closed_at
WHERE resolved_at IS NULL AND closed_at IS NOT NULL;

ALTER TABLE support_cases
    ADD CONSTRAINT support_cases_case_type_valid
    CHECK (case_type IN ('ATTENDEE_ORGANIZER', 'PLATFORM'));

-- Categories were free text before. Normalize legacy values to the enum so
-- the constraint can be fully validated (a NOT VALID constraint would still
-- reject every later UPDATE of such a row, e.g. a status change). Known values
-- map case/space-insensitively; anything else becomes TECHNICAL. The original
-- text is preserved in the append-only audit log before it is replaced.
INSERT INTO audit_logs (entity_type, entity_id, event_id, action, actor_type,
                        before_data, after_data, metadata)
SELECT 'support_case', c.id, c.event_id, 'support_case.category_migrated', 'SYSTEM',
       jsonb_build_object('category', c.category),
       jsonb_build_object('category', norm.category),
       jsonb_build_object('migration', 'migrate_009_support_admin_analytics.sql')
FROM support_cases c
CROSS JOIN LATERAL (
    SELECT CASE
        WHEN upper(regexp_replace(btrim(c.category), '[\s-]+', '_', 'g')) IN (
            'TICKET_DELIVERY', 'PAYMENT', 'REFUND', 'SEATING',
            'EVENT_INFORMATION', 'CHECK_IN', 'ACCOUNT', 'TECHNICAL'
        ) THEN upper(regexp_replace(btrim(c.category), '[\s-]+', '_', 'g'))
        ELSE 'TECHNICAL'
    END AS category
) norm
WHERE c.category IS DISTINCT FROM norm.category;

UPDATE support_cases
SET category = CASE
    WHEN upper(regexp_replace(btrim(category), '[\s-]+', '_', 'g')) IN (
        'TICKET_DELIVERY', 'PAYMENT', 'REFUND', 'SEATING',
        'EVENT_INFORMATION', 'CHECK_IN', 'ACCOUNT', 'TECHNICAL'
    ) THEN upper(regexp_replace(btrim(category), '[\s-]+', '_', 'g'))
    ELSE 'TECHNICAL'
END
WHERE category NOT IN (
    'TICKET_DELIVERY', 'PAYMENT', 'REFUND', 'SEATING',
    'EVENT_INFORMATION', 'CHECK_IN', 'ACCOUNT', 'TECHNICAL'
);

ALTER TABLE support_cases
    ADD CONSTRAINT support_cases_category_valid
    CHECK (category IN (
        'TICKET_DELIVERY', 'PAYMENT', 'REFUND', 'SEATING',
        'EVENT_INFORMATION', 'CHECK_IN', 'ACCOUNT', 'TECHNICAL'
    ));
-- Holds for every backfilled row: ATTENDEE_ORGANIZER was only assigned above
-- when event_id is set.
ALTER TABLE support_cases
    ADD CONSTRAINT support_cases_attendee_has_event
    CHECK (case_type <> 'ATTENDEE_ORGANIZER' OR event_id IS NOT NULL);

CREATE INDEX IF NOT EXISTS idx_support_cases_requester
    ON support_cases (requester_user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_support_cases_event
    ON support_cases (event_id, updated_at DESC)
    WHERE event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_support_cases_assignee
    ON support_cases (assignee_user_id, updated_at DESC)
    WHERE assignee_user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_support_cases_type
    ON support_cases (case_type, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_support_cases_escalated
    ON support_cases (escalated_at DESC)
    WHERE escalated_at IS NOT NULL;

-- Messages: a per-case sequence number gives a strict, deterministic order
-- (and a polling cursor) even when timestamps are identical.
ALTER TABLE support_messages ADD COLUMN IF NOT EXISTS sequence INTEGER;
ALTER TABLE support_messages ADD COLUMN IF NOT EXISTS sender_role VARCHAR(30);
UPDATE support_messages m
SET sequence = ranked.rn
FROM (
    SELECT id, ROW_NUMBER() OVER (PARTITION BY case_id ORDER BY created_at, id) AS rn
    FROM support_messages
) ranked
WHERE m.id = ranked.id AND m.sequence IS NULL;
ALTER TABLE support_messages ALTER COLUMN sequence SET NOT NULL;
ALTER TABLE support_messages
    ADD CONSTRAINT support_messages_sequence_positive CHECK (sequence > 0);
ALTER TABLE support_messages
    ADD CONSTRAINT uq_support_messages_case_sequence UNIQUE (case_id, sequence);
CREATE INDEX IF NOT EXISTS idx_support_messages_case_created
    ON support_messages (case_id, created_at);

-- ---------- In-app notifications (table exists since migrate_001) ----------

CREATE INDEX IF NOT EXISTS idx_notifications_user
    ON notifications (user_id, created_at DESC);

-- ---------- User suspension (enforced through users.status = BLOCKED) ----------

ALTER TABLE users ADD COLUMN IF NOT EXISTS suspended_at TIMESTAMPTZ;
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS suspended_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE users ADD COLUMN IF NOT EXISTS suspension_reason TEXT;
CREATE INDEX IF NOT EXISTS idx_users_created ON users (created_at DESC);

-- ---------- Event suspension (orthogonal to the event lifecycle status) ----------

ALTER TABLE events ADD COLUMN IF NOT EXISTS suspended_at TIMESTAMPTZ;
ALTER TABLE events
    ADD COLUMN IF NOT EXISTS suspended_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE events ADD COLUMN IF NOT EXISTS suspension_reason TEXT;
CREATE INDEX IF NOT EXISTS idx_events_suspended
    ON events (suspended_at DESC)
    WHERE suspended_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_events_starts ON events (starts_at);

-- ---------- Paid-sales stop (per event activation) ----------

ALTER TABLE event_sales_activations ADD COLUMN IF NOT EXISTS sales_suspended_at TIMESTAMPTZ;
ALTER TABLE event_sales_activations
    ADD COLUMN IF NOT EXISTS sales_suspended_by_user_id UUID
    REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE event_sales_activations ADD COLUMN IF NOT EXISTS sales_suspension_reason TEXT;

-- ---------- Payment disputes (minimal admin-recorded entity) ----------

CREATE TABLE IF NOT EXISTS payment_disputes (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_id          UUID NOT NULL REFERENCES payments(id) ON DELETE RESTRICT,
    order_id            UUID NOT NULL REFERENCES orders(id) ON DELETE RESTRICT,
    event_id            UUID NOT NULL REFERENCES events(id) ON DELETE RESTRICT,
    support_case_id     UUID REFERENCES support_cases(id) ON DELETE SET NULL,
    opened_by_user_id   UUID REFERENCES users(id) ON DELETE SET NULL,
    resolved_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    status              VARCHAR(20) NOT NULL DEFAULT 'OPEN',
    reason              TEXT NOT NULL,
    amount              NUMERIC(12,2) NOT NULL,
    currency            CHAR(3) NOT NULL,
    resolution_note     TEXT,
    resolved_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT payment_disputes_status_valid
        CHECK (status IN ('OPEN', 'UNDER_REVIEW', 'RESOLVED', 'REJECTED')),
    CONSTRAINT payment_disputes_amount_positive CHECK (amount > 0),
    CONSTRAINT payment_disputes_reason_not_blank CHECK (btrim(reason) <> '')
);
-- One open dispute per payment.
CREATE UNIQUE INDEX IF NOT EXISTS uq_payment_disputes_active
    ON payment_disputes (payment_id)
    WHERE status IN ('OPEN', 'UNDER_REVIEW');
CREATE INDEX IF NOT EXISTS idx_payment_disputes_status
    ON payment_disputes (status, created_at DESC);
DROP TRIGGER IF EXISTS trg_payment_disputes_updated_at ON payment_disputes;
CREATE TRIGGER trg_payment_disputes_updated_at BEFORE UPDATE ON payment_disputes
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------- Read-path indexes for analytics and monitoring ----------

-- Realized sales by event and payment confirmation time.
CREATE INDEX IF NOT EXISTS idx_orders_event_paid
    ON orders (event_id, paid_at)
    WHERE paid_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_payments_status_created
    ON payments (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_refunds_status_created
    ON refunds (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_order_notifications_failed
    ON order_notifications (updated_at DESC)
    WHERE status = 'FAILED';
-- Administrative action listing.
CREATE INDEX IF NOT EXISTS idx_audit_logs_admin
    ON audit_logs (created_at DESC)
    WHERE actor_type = 'PLATFORM_ADMIN';
