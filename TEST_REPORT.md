# Backend test report — email delivery and organizer audit

## Summary

The backend suite was executed in the project environment with Python 3.10.12,
`pytest`, SQLite in-memory integration tests, and mocked HTTP for the Brevo
adapter. No real email was sent.

| Test module | Passed | Skipped |
|---|---:|---:|
| `tests/test_auth.py` | 26 | 0 |
| `tests/test_checkin_workflows.py` | 11 | 3 |
| `tests/test_email_provider.py` | 14 | 0 |
| `tests/test_errors.py` | 2 | 0 |
| `tests/test_event_staff.py` | 19 | 0 |
| `tests/test_events.py` | 19 | 1 |
| `tests/test_migrations.py` | 6 | 5 |
| `tests/test_orders.py` | 3 | 1 |
| `tests/test_organizers.py` | 16 | 0 |
| `tests/test_paid_sales.py` | 5 | 0 |
| `tests/test_password_reset.py` | 9 | 0 |
| `tests/test_tickets.py` | 8 | 0 |
| **Total** | **138** | **10** |

Verification commands:

```bash
python -m compileall -q app tests
ruff check app tests
mypy
pytest -q
```

`ruff` and `mypy` completed with no issues. The 10 skipped tests require
PostgreSQL row locking or a real PostgreSQL migration target; no PostgreSQL test
server was available in this environment.

## New email-provider coverage

`tests/test_email_provider.py` verifies:

- Brevo requests use `POST /v3/smtp/email`, the `api-key` header, and a valid
  sender/recipient JSON payload.
- User-controlled event, venue, ticket type, attendee, and URL values are
  HTML-escaped.
- HTTP 400/401/403 are permanent failures and are not retried.
- HTTP 429 and 5xx are transient; `Retry-After` is captured.
- Timeouts are transient and marked ambiguous because Brevo may already have
  accepted the message.
- Invalid API keys/senders are rejected.
- `APP_ENV=test` always selects `DevEmailProvider`, even if Brevo settings are
  present.
- Production rejects the developer email provider.
- Sender resolution falls back from `BREVO_SENDER_EMAIL`/`BREVO_SENDER_NAME` to
  `EMAIL_FROM`.

## Notification and organizer coverage

`tests/test_tickets.py` verifies that:

- Permanent notification failures are excluded from retries.
- Transient notification failures respect exponential backoff and increment
  attempts when retried after the delay.
- Existing regression coverage confirms a repeated order confirmation does not
  mint tickets again or send a duplicate email.

`tests/test_organizers.py` verifies that:

- Repeating the same payout PUT updates one row instead of creating duplicates.
- A non-default payout PUT does not demote the existing default account.
- A payout reference owned by another organizer cannot be claimed.
- Organizer profile creation and payout changes remain audited.

## Remaining limitations

- Transport timeouts and 5xx responses are retried at least once, so a provider
  that accepted a message but lost the response may receive a duplicate. The
  outbox has no provider-side idempotency key.
- Permanent outbox failures stay marked `permanent:` until an operator corrects
  the configuration/payload and resets the row manually.
- Email templates provide English copy for all locales; non-English copy is not
  implemented yet.
- Brevo sender authentication and domain verification are deployment
  prerequisites and cannot be tested in the local suite.
