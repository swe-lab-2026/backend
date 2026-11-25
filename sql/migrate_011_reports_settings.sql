-- BiletFlow: reported events and platform settings (SRS 4.12).
-- PostgreSQL 15+. Run AFTER migrate_010_organizer_refunds.sql. The runner
-- wraps the file in one transaction and records it exactly once.
--
-- Additive: two new tables, nothing existing is changed.

-- ---------- Reported events ----------

CREATE TABLE IF NOT EXISTS event_reports (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id            UUID NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    reporter_user_id    UUID REFERENCES users(id) ON DELETE SET NULL,
    reason              VARCHAR(30) NOT NULL,
    details             TEXT,
    status              VARCHAR(20) NOT NULL DEFAULT 'OPEN',
    resolution_note     TEXT,
    resolved_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    resolved_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT event_reports_reason_valid
        CHECK (reason IN ('FRAUD', 'MISLEADING', 'INAPPROPRIATE', 'PROHIBITED', 'SPAM', 'OTHER')),
    CONSTRAINT event_reports_status_valid
        CHECK (status IN ('OPEN', 'UNDER_REVIEW', 'RESOLVED', 'DISMISSED'))
);
-- One active report per reporter and event.
CREATE UNIQUE INDEX IF NOT EXISTS uq_event_reports_active
    ON event_reports (event_id, reporter_user_id)
    WHERE status IN ('OPEN', 'UNDER_REVIEW');
CREATE INDEX IF NOT EXISTS idx_event_reports_status
    ON event_reports (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_event_reports_event
    ON event_reports (event_id, created_at DESC);
DROP TRIGGER IF EXISTS trg_event_reports_updated_at ON event_reports;
CREATE TRIGGER trg_event_reports_updated_at BEFORE UPDATE ON event_reports
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------- Platform settings (single row, id = 1) ----------

CREATE TABLE IF NOT EXISTS platform_settings (
    id                      SMALLINT PRIMARY KEY DEFAULT 1,
    activation_fee_amount   NUMERIC(12,2) NOT NULL DEFAULT 1000.00,
    activation_fee_currency CHAR(3) NOT NULL DEFAULT 'KZT',
    processing_fee_percent  NUMERIC(5,2) NOT NULL DEFAULT 3.00,
    updated_by_user_id      UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT platform_settings_singleton CHECK (id = 1),
    CONSTRAINT platform_settings_fee_nonnegative CHECK (activation_fee_amount >= 0),
    CONSTRAINT platform_settings_percent_range
        CHECK (processing_fee_percent >= 0 AND processing_fee_percent <= 20)
);
-- Seed with the values that were hard-coded before (1000 KZT, 3%).
INSERT INTO platform_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
