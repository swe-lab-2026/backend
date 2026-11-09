-- Event-specific paid-sales activation and idempotent internal payment simulation.
CREATE TABLE event_sales_activations (
    event_id UUID PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    activation_fee_amount NUMERIC(12,2) NOT NULL DEFAULT 1000.00
        CHECK (activation_fee_amount >= 0),
    activation_fee_paid_at TIMESTAMPTZ,
    terms_accepted_at TIMESTAMPTZ,
    activated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX uq_simulated_payment_per_order
    ON payments(order_id) WHERE provider = 'internal_simulation';
