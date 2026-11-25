-- BiletFlow: organizer-initiated full refunds (SRS 4.9).
-- PostgreSQL 15+. Run AFTER migrate_009_support_admin_analytics.sql. The
-- runner wraps the file in one transaction and records it exactly once.
--
-- The event owner may now refund one order of their own event; those refunds
-- are stored with kind ORGANIZER_REQUEST. Existing rows already satisfy the
-- widened constraint, so nothing is rewritten.

ALTER TABLE refunds DROP CONSTRAINT IF EXISTS refunds_kind_valid;
ALTER TABLE refunds
    ADD CONSTRAINT refunds_kind_valid
    CHECK (kind IN ('CUSTOMER_REQUEST', 'EVENT_CANCELLATION', 'ORGANIZER_REQUEST'));
