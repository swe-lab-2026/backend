-- BiletFlow SCRUM-15: promo-code campaigns, atomic redemption accounting and
-- order campaign attribution.
-- PostgreSQL 15+. Run AFTER migrate_006_paid_sales.sql. The runner wraps the
-- whole file in one transaction and records it exactly once.
--
-- Builds on tables that already exist since migrate_001 (campaigns,
-- campaign_ticket_types, promo_redemptions, orders.campaign_id). Only additive,
-- backward-compatible changes: new columns have defaults, nothing is dropped.

-- Capacity currently held by a campaign (RESERVED + REDEEMED redemptions).
-- Incremented with a conditional UPDATE at checkout; the CHECK below makes
-- PostgreSQL itself refuse to exceed max_redemptions.
ALTER TABLE campaigns
    ADD COLUMN IF NOT EXISTS redemption_count INTEGER NOT NULL DEFAULT 0;

-- Redemption lifecycle. Existing rows (none are created by earlier code) are
-- treated as completed redemptions.
ALTER TABLE promo_redemptions
    ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE promo_redemptions
    ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'REDEEMED';
ALTER TABLE promo_redemptions
    ADD COLUMN IF NOT EXISTS released_at TIMESTAMPTZ;
ALTER TABLE promo_redemptions
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE promo_redemptions
    ADD CONSTRAINT promo_redemptions_status_valid
    CHECK (status IN ('RESERVED', 'REDEEMED', 'RELEASED'));

-- Historical promo code snapshot on the order (attribution survives campaign
-- edits/disablement).
ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS promo_code_snapshot VARCHAR(40);

-- Backfill the counter from any pre-existing redemptions before the
-- constraint is added.
UPDATE campaigns c
SET redemption_count = (
    SELECT COUNT(*) FROM promo_redemptions r
    WHERE r.campaign_id = c.id AND r.status IN ('RESERVED', 'REDEEMED')
);

ALTER TABLE campaigns
    ADD CONSTRAINT campaigns_redemption_count_valid
    CHECK (
        redemption_count >= 0
        AND (max_redemptions IS NULL OR redemption_count <= max_redemptions)
    );

-- At most one in-flight (RESERVED) checkout per customer per campaign.
CREATE UNIQUE INDEX IF NOT EXISTS uq_promo_redemptions_active_reservation
    ON promo_redemptions (campaign_id, user_id)
    WHERE status = 'RESERVED';

CREATE INDEX IF NOT EXISTS idx_promo_redemptions_campaign_status
    ON promo_redemptions (campaign_id, status);

CREATE INDEX IF NOT EXISTS idx_orders_campaign
    ON orders (campaign_id)
    WHERE campaign_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_campaigns_event_status
    ON campaigns (event_id, status);
