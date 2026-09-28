-- BiletFlow database schema
-- Target: PostgreSQL 15+
-- Generated for the Academic MVP.
-- Run on an empty database with a role allowed to create extensions.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

-- ---------- Enums ----------

DO $$ BEGIN
    CREATE TYPE user_role AS ENUM ('USER', 'PLATFORM_ADMIN');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    CREATE TYPE user_status AS ENUM ('ACTIVE', 'BLOCKED', 'DELETED');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    CREATE TYPE verification_status AS ENUM ('NOT_SUBMITTED', 'PENDING', 'VERIFIED', 'REJECTED');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    CREATE TYPE payout_status AS ENUM ('PENDING', 'ACTIVE', 'BLOCKED');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    CREATE TYPE event_visibility AS ENUM ('PUBLIC', 'UNLISTED', 'PRIVATE');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    CREATE TYPE event_status AS ENUM ('DRAFT', 'PUBLISHED', 'UNPUBLISHED', 'CANCELLED', 'COMPLETED');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    CREATE TYPE event_staff_role AS ENUM ('EVENT_ADMIN', 'ORGANIZER_STAFF', 'CHECK_IN_STAFF');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    CREATE TYPE paid_sales_status AS ENUM ('NOT_REQUESTED', 'PENDING', 'ACTIVE', 'REJECTED', 'SUSPENDED');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    CREATE TYPE order_status AS ENUM ('PENDING', 'PAID', 'CANCELLED', 'EXPIRED', 'PARTIALLY_REFUNDED', 'REFUNDED');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    CREATE TYPE hold_status AS ENUM ('ACTIVE', 'CONSUMED', 'EXPIRED', 'RELEASED');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    CREATE TYPE ticket_status AS ENUM ('VALID', 'CHECKED_IN', 'CANCELLED', 'REFUNDED');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    CREATE TYPE payment_status AS ENUM ('PENDING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'REFUNDED', 'PARTIALLY_REFUNDED');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    CREATE TYPE refund_status AS ENUM ('PENDING', 'SUCCEEDED', 'FAILED', 'CANCELLED');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    CREATE TYPE discount_type AS ENUM ('PERCENT', 'FIXED');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    CREATE TYPE campaign_status AS ENUM ('DRAFT', 'ACTIVE', 'PAUSED', 'EXPIRED');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    CREATE TYPE checkin_action AS ENUM ('CHECK_IN', 'REVERSE');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    CREATE TYPE support_case_status AS ENUM ('OPEN', 'IN_PROGRESS', 'RESOLVED', 'CLOSED');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    CREATE TYPE notification_channel AS ENUM ('EMAIL', 'PUSH', 'IN_APP');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    CREATE TYPE notification_status AS ENUM ('PENDING', 'SENT', 'FAILED', 'READ');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    CREATE TYPE event_seat_status AS ENUM ('AVAILABLE', 'HELD', 'SOLD', 'BLOCKED');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ---------- Shared trigger ----------

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

-- ---------- Identity and organizers ----------

CREATE TABLE users (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email               CITEXT NOT NULL UNIQUE,
    password_hash       TEXT NOT NULL,
    first_name          VARCHAR(100) NOT NULL,
    last_name           VARCHAR(100) NOT NULL,
    phone               VARCHAR(32),
    locale              VARCHAR(10) NOT NULL DEFAULT 'en',
    global_role         user_role NOT NULL DEFAULT 'USER',
    status              user_status NOT NULL DEFAULT 'ACTIVE',
    email_verified_at   TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT users_email_not_blank CHECK (btrim(email::TEXT) <> '')
);

CREATE TABLE organizer_profiles (
    user_id             UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    display_name        VARCHAR(160) NOT NULL,
    legal_name          VARCHAR(200),
    contact_email       CITEXT,
    contact_phone       VARCHAR(32),
    verification_status verification_status NOT NULL DEFAULT 'NOT_SUBMITTED',
    terms_accepted_at   TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE payout_accounts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organizer_user_id   UUID NOT NULL REFERENCES organizer_profiles(user_id) ON DELETE CASCADE,
    provider            VARCHAR(50) NOT NULL,
    external_account_ref TEXT NOT NULL,
    status              payout_status NOT NULL DEFAULT 'PENDING',
    is_default          BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (provider, external_account_ref)
);

CREATE UNIQUE INDEX uq_payout_accounts_default
    ON payout_accounts (organizer_user_id)
    WHERE is_default;

-- ---------- Events and inventory ----------

CREATE TABLE events (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id       UUID NOT NULL REFERENCES organizer_profiles(user_id) ON DELETE RESTRICT,
    title               VARCHAR(200) NOT NULL,
    description         TEXT,
    category            VARCHAR(80),
    visibility          event_visibility NOT NULL DEFAULT 'PUBLIC',
    status              event_status NOT NULL DEFAULT 'DRAFT',
    venue_name          VARCHAR(200),
    venue_address       TEXT,
    city                VARCHAR(120),
    country_code        CHAR(2),
    latitude            NUMERIC(9,6),
    longitude           NUMERIC(9,6),
    timezone            VARCHAR(64) NOT NULL,
    starts_at           TIMESTAMPTZ NOT NULL,
    ends_at             TIMESTAMPTZ NOT NULL,
    registration_starts_at TIMESTAMPTZ,
    registration_ends_at   TIMESTAMPTZ,
    capacity            INTEGER,
    refund_policy       TEXT,
    published_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT events_time_order CHECK (ends_at > starts_at),
    CONSTRAINT events_capacity_positive CHECK (capacity IS NULL OR capacity > 0),
    CONSTRAINT events_latitude_range CHECK (latitude IS NULL OR latitude BETWEEN -90 AND 90),
    CONSTRAINT events_longitude_range CHECK (longitude IS NULL OR longitude BETWEEN -180 AND 180),
    CONSTRAINT events_registration_order CHECK (
        registration_starts_at IS NULL OR registration_ends_at IS NULL
        OR registration_ends_at > registration_starts_at
    )
);

CREATE TABLE event_images (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id            UUID NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    object_key          TEXT NOT NULL,
    alt_text            VARCHAR(240),
    sort_order          SMALLINT NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (event_id, object_key)
);

CREATE TABLE event_staff (
    event_id            UUID NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role                event_staff_role NOT NULL,
    permissions         JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (event_id, user_id)
);

CREATE TABLE ticket_types (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id            UUID NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name                VARCHAR(120) NOT NULL,
    description         TEXT,
    price               NUMERIC(12,2) NOT NULL DEFAULT 0,
    currency            CHAR(3) NOT NULL DEFAULT 'KZT',
    capacity            INTEGER NOT NULL,
    min_per_order       INTEGER NOT NULL DEFAULT 1,
    max_per_order       INTEGER NOT NULL DEFAULT 10,
    sales_starts_at     TIMESTAMPTZ,
    sales_ends_at       TIMESTAMPTZ,
    is_hidden           BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ticket_types_price_nonnegative CHECK (price >= 0),
    CONSTRAINT ticket_types_capacity_positive CHECK (capacity > 0),
    CONSTRAINT ticket_types_order_limits CHECK (min_per_order > 0 AND max_per_order >= min_per_order),
    CONSTRAINT ticket_types_sales_order CHECK (
        sales_starts_at IS NULL OR sales_ends_at IS NULL OR sales_ends_at > sales_starts_at
    ),
    UNIQUE (event_id, name)
);

CREATE TABLE paid_sales_activations (
    event_id            UUID PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    payout_account_id   UUID REFERENCES payout_accounts(id) ON DELETE RESTRICT,
    status              paid_sales_status NOT NULL DEFAULT 'NOT_REQUESTED',
    activation_fee      NUMERIC(12,2) NOT NULL DEFAULT 0,
    terms_accepted_at   TIMESTAMPTZ,
    activated_at        TIMESTAMPTZ,
    reviewed_by         UUID REFERENCES users(id) ON DELETE SET NULL,
    review_note         TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT paid_sales_fee_nonnegative CHECK (activation_fee >= 0)
);

-- ---------- Promotions ----------

CREATE TABLE campaigns (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id            UUID NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name                VARCHAR(160) NOT NULL,
    promo_code          CITEXT,
    campaign_token      UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    discount_type       discount_type NOT NULL,
    discount_value      NUMERIC(12,2) NOT NULL,
    starts_at           TIMESTAMPTZ,
    ends_at             TIMESTAMPTZ,
    max_redemptions     INTEGER,
    status              campaign_status NOT NULL DEFAULT 'DRAFT',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT campaigns_discount_positive CHECK (discount_value > 0),
    CONSTRAINT campaigns_percent_range CHECK (discount_type <> 'PERCENT' OR discount_value <= 100),
    CONSTRAINT campaigns_redemptions_positive CHECK (max_redemptions IS NULL OR max_redemptions > 0),
    CONSTRAINT campaigns_time_order CHECK (starts_at IS NULL OR ends_at IS NULL OR ends_at > starts_at),
    UNIQUE (event_id, promo_code)
);

CREATE TABLE campaign_ticket_types (
    campaign_id         UUID NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    ticket_type_id      UUID NOT NULL REFERENCES ticket_types(id) ON DELETE CASCADE,
    PRIMARY KEY (campaign_id, ticket_type_id)
);

-- ---------- Orders, attendees, and tickets ----------

CREATE TABLE orders (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_number        BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
    event_id            UUID NOT NULL REFERENCES events(id) ON DELETE RESTRICT,
    buyer_user_id       UUID REFERENCES users(id) ON DELETE SET NULL,
    buyer_email         CITEXT NOT NULL,
    status              order_status NOT NULL DEFAULT 'PENDING',
    currency            CHAR(3) NOT NULL DEFAULT 'KZT',
    subtotal_amount     NUMERIC(12,2) NOT NULL DEFAULT 0,
    discount_amount     NUMERIC(12,2) NOT NULL DEFAULT 0,
    fee_amount          NUMERIC(12,2) NOT NULL DEFAULT 0,
    total_amount        NUMERIC(12,2) NOT NULL DEFAULT 0,
    campaign_id         UUID REFERENCES campaigns(id) ON DELETE SET NULL,
    expires_at          TIMESTAMPTZ,
    paid_at             TIMESTAMPTZ,
    cancelled_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT orders_amounts_nonnegative CHECK (
        subtotal_amount >= 0 AND discount_amount >= 0 AND fee_amount >= 0 AND total_amount >= 0
    ),
    CONSTRAINT orders_total_formula CHECK (
        total_amount = subtotal_amount - discount_amount + fee_amount
    )
);

CREATE TABLE attendees (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID REFERENCES users(id) ON DELETE SET NULL,
    full_name           VARCHAR(200) NOT NULL,
    email               CITEXT NOT NULL,
    phone               VARCHAR(32),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- One row represents one ticket unit. This simplifies attendee and QR assignment.
CREATE TABLE order_items (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id            UUID NOT NULL REFERENCES orders(id) ON DELETE RESTRICT,
    ticket_type_id      UUID NOT NULL REFERENCES ticket_types(id) ON DELETE RESTRICT,
    attendee_id         UUID REFERENCES attendees(id) ON DELETE SET NULL,
    ticket_type_name    VARCHAR(120) NOT NULL,
    unit_price          NUMERIC(12,2) NOT NULL,
    discount_amount     NUMERIC(12,2) NOT NULL DEFAULT 0,
    final_price         NUMERIC(12,2) NOT NULL,
    seat_label_snapshot VARCHAR(120),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT order_items_amounts_nonnegative CHECK (
        unit_price >= 0 AND discount_amount >= 0 AND final_price >= 0
    ),
    CONSTRAINT order_items_price_formula CHECK (final_price = unit_price - discount_amount)
);

CREATE TABLE inventory_holds (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id            UUID NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    ticket_type_id      UUID NOT NULL REFERENCES ticket_types(id) ON DELETE CASCADE,
    user_id             UUID REFERENCES users(id) ON DELETE SET NULL,
    order_id            UUID REFERENCES orders(id) ON DELETE CASCADE,
    quantity            INTEGER NOT NULL,
    status              hold_status NOT NULL DEFAULT 'ACTIVE',
    expires_at          TIMESTAMPTZ NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT inventory_holds_quantity_positive CHECK (quantity > 0)
);

CREATE TABLE tickets (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_item_id       UUID NOT NULL UNIQUE REFERENCES order_items(id) ON DELETE RESTRICT,
    public_id           UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    qr_token_hash       TEXT NOT NULL UNIQUE,
    status              ticket_status NOT NULL DEFAULT 'VALID',
    issued_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    checked_in_at       TIMESTAMPTZ,
    cancelled_at        TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE payments (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id            UUID NOT NULL REFERENCES orders(id) ON DELETE RESTRICT,
    provider            VARCHAR(50) NOT NULL,
    provider_payment_ref TEXT,
    amount              NUMERIC(12,2) NOT NULL,
    currency            CHAR(3) NOT NULL,
    status              payment_status NOT NULL DEFAULT 'PENDING',
    is_simulated        BOOLEAN NOT NULL DEFAULT FALSE,
    failure_code        VARCHAR(100),
    failure_message     TEXT,
    paid_at             TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT payments_amount_positive CHECK (amount > 0),
    UNIQUE (provider, provider_payment_ref)
);

CREATE TABLE refunds (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_id          UUID NOT NULL REFERENCES payments(id) ON DELETE RESTRICT,
    order_id            UUID NOT NULL REFERENCES orders(id) ON DELETE RESTRICT,
    initiated_by        UUID REFERENCES users(id) ON DELETE SET NULL,
    amount              NUMERIC(12,2) NOT NULL,
    reason              TEXT,
    provider_refund_ref TEXT,
    status              refund_status NOT NULL DEFAULT 'PENDING',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT refunds_amount_positive CHECK (amount > 0)
);

CREATE TABLE promo_redemptions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id         UUID NOT NULL REFERENCES campaigns(id) ON DELETE RESTRICT,
    order_id            UUID NOT NULL UNIQUE REFERENCES orders(id) ON DELETE RESTRICT,
    discount_amount     NUMERIC(12,2) NOT NULL,
    redeemed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT promo_redemptions_nonnegative CHECK (discount_amount >= 0)
);

-- ---------- Check-in, support, notifications, audit ----------

CREATE TABLE checkin_records (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id           UUID NOT NULL REFERENCES tickets(id) ON DELETE RESTRICT,
    event_id            UUID NOT NULL REFERENCES events(id) ON DELETE RESTRICT,
    performed_by        UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    action              checkin_action NOT NULL,
    device_info         JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE support_cases (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    requester_user_id   UUID REFERENCES users(id) ON DELETE SET NULL,
    assignee_user_id    UUID REFERENCES users(id) ON DELETE SET NULL,
    event_id            UUID REFERENCES events(id) ON DELETE SET NULL,
    order_id            UUID REFERENCES orders(id) ON DELETE SET NULL,
    ticket_id           UUID REFERENCES tickets(id) ON DELETE SET NULL,
    category            VARCHAR(80) NOT NULL,
    subject             VARCHAR(240) NOT NULL,
    status              support_case_status NOT NULL DEFAULT 'OPEN',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at           TIMESTAMPTZ
);

CREATE TABLE support_messages (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id             UUID NOT NULL REFERENCES support_cases(id) ON DELETE CASCADE,
    sender_user_id      UUID REFERENCES users(id) ON DELETE SET NULL,
    body                TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT support_messages_body_not_blank CHECK (btrim(body) <> '')
);

CREATE TABLE notifications (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type                VARCHAR(100) NOT NULL,
    channel             notification_channel NOT NULL,
    payload             JSONB NOT NULL DEFAULT '{}'::JSONB,
    status              notification_status NOT NULL DEFAULT 'PENDING',
    scheduled_at        TIMESTAMPTZ,
    sent_at             TIMESTAMPTZ,
    read_at             TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE audit_logs (
    id                  BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    actor_user_id       UUID REFERENCES users(id) ON DELETE SET NULL,
    event_id            UUID REFERENCES events(id) ON DELETE SET NULL,
    entity_type         VARCHAR(100) NOT NULL,
    entity_id           UUID,
    action              VARCHAR(100) NOT NULL,
    before_data         JSONB,
    after_data          JSONB,
    ip_address          INET,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------- Optional module: assigned seating ----------

CREATE TABLE venues (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id       UUID NOT NULL REFERENCES organizer_profiles(user_id) ON DELETE RESTRICT,
    name                VARCHAR(200) NOT NULL,
    address             TEXT,
    city                VARCHAR(120),
    country_code        CHAR(2),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE venue_layouts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    venue_id            UUID NOT NULL REFERENCES venues(id) ON DELETE CASCADE,
    name                VARCHAR(160) NOT NULL,
    version             INTEGER NOT NULL DEFAULT 1,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (venue_id, name, version)
);

CREATE TABLE venue_sections (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    layout_id           UUID NOT NULL REFERENCES venue_layouts(id) ON DELETE CASCADE,
    name                VARCHAR(120) NOT NULL,
    sort_order          INTEGER NOT NULL DEFAULT 0,
    UNIQUE (layout_id, name)
);

CREATE TABLE venue_rows (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    section_id          UUID NOT NULL REFERENCES venue_sections(id) ON DELETE CASCADE,
    label               VARCHAR(40) NOT NULL,
    sort_order          INTEGER NOT NULL DEFAULT 0,
    UNIQUE (section_id, label)
);

CREATE TABLE seats (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    row_id              UUID NOT NULL REFERENCES venue_rows(id) ON DELETE CASCADE,
    label               VARCHAR(40) NOT NULL,
    x_position          NUMERIC(10,3),
    y_position          NUMERIC(10,3),
    UNIQUE (row_id, label)
);

CREATE TABLE event_seats (
    event_id            UUID NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    seat_id             UUID NOT NULL REFERENCES seats(id) ON DELETE RESTRICT,
    ticket_type_id      UUID NOT NULL REFERENCES ticket_types(id) ON DELETE RESTRICT,
    status              event_seat_status NOT NULL DEFAULT 'AVAILABLE',
    price_override      NUMERIC(12,2),
    PRIMARY KEY (event_id, seat_id),
    CONSTRAINT event_seats_price_nonnegative CHECK (price_override IS NULL OR price_override >= 0)
);

CREATE TABLE seat_holds (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id            UUID NOT NULL,
    seat_id             UUID NOT NULL,
    user_id             UUID REFERENCES users(id) ON DELETE SET NULL,
    order_id            UUID REFERENCES orders(id) ON DELETE CASCADE,
    status              hold_status NOT NULL DEFAULT 'ACTIVE',
    expires_at          TIMESTAMPTZ NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (event_id, seat_id) REFERENCES event_seats(event_id, seat_id) ON DELETE CASCADE
);

-- Prevent two currently active holds for one seat.
CREATE UNIQUE INDEX uq_seat_holds_active
    ON seat_holds (event_id, seat_id)
    WHERE status = 'ACTIVE';

-- ---------- Performance indexes ----------

CREATE INDEX idx_events_public_catalog
    ON events (starts_at, status)
    WHERE visibility = 'PUBLIC' AND status = 'PUBLISHED';
CREATE INDEX idx_events_owner ON events (owner_user_id, created_at DESC);
CREATE INDEX idx_event_staff_user ON event_staff (user_id);
CREATE INDEX idx_ticket_types_event ON ticket_types (event_id);
CREATE INDEX idx_campaigns_event_status ON campaigns (event_id, status);
CREATE INDEX idx_orders_buyer ON orders (buyer_user_id, created_at DESC);
CREATE INDEX idx_orders_event_status ON orders (event_id, status, created_at DESC);
CREATE INDEX idx_order_items_order ON order_items (order_id);
CREATE INDEX idx_inventory_holds_active
    ON inventory_holds (ticket_type_id, expires_at)
    WHERE status = 'ACTIVE';
CREATE INDEX idx_payments_order ON payments (order_id);
CREATE INDEX idx_refunds_order ON refunds (order_id);
CREATE INDEX idx_checkin_records_ticket ON checkin_records (ticket_id, created_at DESC);
CREATE INDEX idx_checkin_records_event ON checkin_records (event_id, created_at DESC);
CREATE INDEX idx_support_cases_status ON support_cases (status, created_at DESC);
CREATE INDEX idx_notifications_pending
    ON notifications (scheduled_at, created_at)
    WHERE status = 'PENDING';
CREATE INDEX idx_audit_logs_entity ON audit_logs (entity_type, entity_id, created_at DESC);

-- ---------- updated_at triggers ----------

CREATE TRIGGER trg_users_updated_at BEFORE UPDATE ON users
FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_organizer_profiles_updated_at BEFORE UPDATE ON organizer_profiles
FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_payout_accounts_updated_at BEFORE UPDATE ON payout_accounts
FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_events_updated_at BEFORE UPDATE ON events
FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_ticket_types_updated_at BEFORE UPDATE ON ticket_types
FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_paid_sales_activations_updated_at BEFORE UPDATE ON paid_sales_activations
FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_campaigns_updated_at BEFORE UPDATE ON campaigns
FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_orders_updated_at BEFORE UPDATE ON orders
FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_inventory_holds_updated_at BEFORE UPDATE ON inventory_holds
FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_tickets_updated_at BEFORE UPDATE ON tickets
FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_payments_updated_at BEFORE UPDATE ON payments
FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_refunds_updated_at BEFORE UPDATE ON refunds
FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_support_cases_updated_at BEFORE UPDATE ON support_cases
FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_venues_updated_at BEFORE UPDATE ON venues
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMIT;

-- Transaction notes for the backend:
-- 1. Checkout must lock the relevant ticket_types row with SELECT ... FOR UPDATE.
-- 2. Available quantity = capacity - sold tickets - non-expired ACTIVE holds.
-- 3. Check-in must lock the tickets row before switching VALID -> CHECKED_IN.
-- 4. Store only a hash in tickets.qr_token_hash; never store the raw QR secret.
-- 5. Financial, ticket, check-in, and audit records should not be hard-deleted.
