# BiletFlow — backend

BiletFlow is a student project: a platform for creating events, selling and issuing
tickets, managing orders and checking QR codes at the door. This repository is the
REST API that the [web app](https://github.com/swe-lab-2026/frontend) and the
[mobile app](https://github.com/swe-lab-2026/mobile) both use.

**Stack:** Python 3.10+, FastAPI, SQLAlchemy (async, `asyncpg`), PostgreSQL 17,
Redis 8 (optional locally), Pydantic settings, Brevo for email, Docker Compose.
FastAPI generates the OpenAPI docs at `/docs` and `/openapi.json`.

## Quick start

### Local (Python)

```bash
cp .env.example .env          # set JWT_ACCESS_SECRET, JWT_REFRESH_SECRET, TICKET_QR_SECRET
make install                  # pip install -e ".[dev]"
make infra                    # docker compose up -d postgres redis (or use your own PostgreSQL)
make migrate                  # apply sql/migrate_*.sql (also runs on startup)
make run                      # uvicorn on http://localhost:8000
```

With `EMAIL_PROVIDER=dev` (the default) no email is sent: verification codes and
reset links are written to the log (`verification code for <email>: 123456`).

### Docker Compose

```bash
cp .env.example .env          # also set POSTGRES_PASSWORD
docker compose up -d --build  # postgres + redis + api on http://localhost:8000
```

The `api` container's health check calls `/health/ready`. Uploaded images are
kept in the `media_data` volume.

### First Platform Admin

There is no HTTP route that creates a `PLATFORM_ADMIN`. Register and verify an
account through the API, then run `make admin EMAIL=you@example.com`
(in Docker: `docker compose exec api python -m app.db.bootstrap_admin --email ...`).

### Make targets

| Command | What it does |
|---|---|
| `make install` | Install the package with dev tools (pytest, ruff, mypy) |
| `make infra` | Start PostgreSQL and Redis in Docker |
| `make run` | Run the API with auto-reload on port 8000 |
| `make migrate` | Apply versioned SQL migrations to `DATABASE_URL` |
| `make admin EMAIL=...` | Promote a verified account to `PLATFORM_ADMIN` |
| `make lint` / `make typecheck` / `make test` | ruff, mypy, pytest |
| `make check` | lint + typecheck + test |

## Implemented features

| Area | What works |
|---|---|
| Auth | Email + password registration, 6-digit email verification code (resend with cooldown and hourly limit), login with lockout after failed attempts, rotating refresh tokens with reuse detection, logout, log out of all sessions, password reset by email link or token |
| Users | Own profile, interface/email language (`en`, `ru`, `kk`), in-app notifications with read state, own event reports |
| Organizers | Organizer profile, verification status, demo payout account (masked reference), own events list, organizer-wide analytics, demo self-verification and payout approval |
| Events | Draft → publish / unpublish → cancel → completed lifecycle, edit, delete draft, duplicate, publish checklist preview, public catalog, timezones, image upload, `.ics` calendar export, activity timeline, audit log, event reports |
| Event staff | Event-scoped `EVENT_ADMIN` / `CHECK_IN_STAFF` assignment by email, "assigned events" list with caller-specific `can_reverse_check_in` |
| Ticket types & inventory | Create / edit / delete ticket types, price, capacity, per-order limits, sale window, hidden types, event-level capacity, live availability, transactional holds |
| Orders | Free registration (instant `PAID`, 0.00 KZT), paid orders with a 10-minute reservation, named attendees per ticket, processing fee, automatic expiry of unpaid orders |
| Paid sales | Per-event activation checklist, terms, demo activation fee, simulated payments (success / failed / abandoned), idempotent confirmation |
| Tickets | One ticket per attendee, signed QR payload (only its hash is stored), ticket list, ticket details, PDF download, tickets sent by email |
| Check-in | QR validation and check-in, manual check-in by ticket id, reverse check-in, server-side attendee search, attendance stats, machine-readable `admission_status`, row locking against double entry |
| Campaigns | Promo codes (percent / fixed), validity window, redemption limit, ticket-type scope, server-side quote, campaign QR codes (PNG) that redirect to the event page, per-campaign analytics |
| Refunds | Event refund policy, refund eligibility, buyer cancellation / refund, organizer full refund, refunds of a cancelled event in batches, simulated refunds |
| Notifications | Brevo email with retries and an outbox; localized templates for verification, password reset, order confirmation, failed payment, event change / cancellation and refunds; in-app notifications |
| Support | Support cases with event / order / ticket context, messages with polling, assignment, status changes, escalation to Platform Admins, activity log |
| Administration | Users, events, orders, payments, paid-sales activations, refunds, failures, escalated support, campaigns, disputes, payout accounts, organizer verification, reports, platform fees, admin action log, suspensions, server-side CSV export |
| Analytics | Event KPIs, revenue, sales over time, attendance, campaign performance, organizer totals |
| Operations | `/health/live`, `/health/ready`, request logging, CI (lint, typecheck, tests on SQLite and PostgreSQL 17), SSH deploy of verified `main` commits |

The sections below describe the architecture first and then each feature in detail.

---

## 1. Purpose of this document

This document describes the backend architecture of BiletFlow.

Project stack:

- **Backend:** Python + FastAPI;
- **Web frontend:** Next.js;
- **Mobile:** React Native (Expo);
- **Database:** PostgreSQL;
- **API:** REST/JSON;
- **API documentation:** OpenAPI, generated automatically by FastAPI.

The Academic MVP is a **modular monolith**. All backend modules run inside one
FastAPI application and one PostgreSQL database, but the code is split by
business area. For a student team this is simpler than microservices, and
individual modules can still be moved into separate services later.

---

## 2. System overview

```mermaid
flowchart TD
    WEB[Next.js Web App]
    MOBILE[React Native App]
    API[FastAPI REST API]
    DB[(PostgreSQL)]
    KV[(Redis)]
    EMAIL[Brevo email]
    PAY[Payment simulator]
    FILES[Media storage]

    WEB -->|HTTPS + JSON| API
    MOBILE -->|HTTPS + JSON| API
    API -->|SQL transactions| DB
    API -->|Codes, rate limits| KV
    API -->|Verification and tickets| EMAIL
    API -->|Simulated payments| PAY
    API -->|Event images| FILES
```

FastAPI is the only way to reach the data. Next.js and React Native never
connect to PostgreSQL directly.

Redis holds short-lived data only: email-code challenges, rate limits and daily
counters (`app/kv/store.py`). When `REDIS_URL` is empty an in-process store is
used, so local development and tests need no extra infrastructure.

---

## 3. Architectural style

### 3.1. Modular monolith

Each business module has its own API routes, request/response schemas,
business logic and database access, but all modules run as one application.

Main benefits for BiletFlow:

- one repository and a simple local setup;
- one PostgreSQL transaction for an order, its payment and its tickets;
- less infrastructure and DevOps work;
- a clear split of work between team members;
- Payments, Notifications or Check-in can be moved into a separate service later.

### 3.2. Backend layers

| Layer | Responsibility | Location |
|---|---|---|
| API | HTTP routes, parameters, DTOs, status codes | `app/api/routes/*`, `app/schemas/*` |
| Service | Business rules and use cases | `app/services/*` |
| Repository | PostgreSQL queries and transactions | `app/db/repositories.py` |
| Model | Tables, entities and data constraints | `app/db/models.py`, `sql/*` |

An API route does not run complex SQL or compute ticket availability itself. It
validates the input, calls a service and turns the result into an HTTP response.

```mermaid
flowchart LR
    ROUTER[API Router] --> SERVICE[Service]
    SERVICE --> REPO[Repository]
    REPO --> DB[(PostgreSQL)]
```

---

## 4. Project structure

```text
backend/
├── app/
│   ├── main.py               app factory, routers, health checks, background loops
│   ├── api/
│   │   ├── deps.py           auth and permission dependencies
│   │   └── routes/           auth, users, organizers, events, campaigns, orders,
│   │                         tickets (+ check-in), support, admin, media
│   ├── core/                 config, errors, logging, security (hashing, JWT)
│   ├── db/                   models, repositories, session, schema, migrate,
│   │                         bootstrap_admin
│   ├── kv/                   TTL key-value store (Redis or in-process)
│   ├── schemas/              Pydantic request/response models per module
│   └── services/             business logic: auth, events, inventory, orders,
│                             paid_sales, tickets, pdf_tickets, campaigns, refunds,
│                             notifications, email, email_templates, support,
│                             reports, analytics, admin, platform_settings,
│                             media, calendar, organizers, event_staff, ticket_types
├── sql/                      versioned migrations migrate_001 … migrate_011
├── tests/                    pytest suite (SQLite by default, PostgreSQL in CI)
├── scripts/                  benchmark_qr_validation.py
├── .github/workflows/        ci.yml, deploy.yml
├── docker-compose.yml        postgres, redis, api
├── Dockerfile
├── Makefile
└── pyproject.toml
```

A service may use `core`, the database session and the public functions of
other services. Tables that belong to another module are changed only through
that module's service or an agreed repository function.

---

## 5. Backend modules

### 5.1. Auth

Responsible for:

- registration with email and password;
- email verification;
- login and logout;
- access and refresh tokens;
- refreshing the access token;
- password reset;
- ending all active sessions;
- limiting failed login attempts.

### 5.2. Users

User profile, contact details, interface language, account status and global role.

### 5.3. Organizers

Creates the organizer profile and stores its details and verification status.
One user can be a regular buyer and an organizer at the same time.

### 5.4. Events

Creating, editing, publishing and cancelling events, together with their
description, venue, time, images and staff.

### 5.5. Ticket types and inventory

Ticket categories, price, limits and remaining stock. Remaining stock is not
stored as a separate mutable number:

```text
available = capacity - sold - active_holds
```

The event-wide limit (`events.capacity`) caps all ticket types together. In
`GET /api/v1/events/{event_id}/inventory` each type has `type_remaining` (what
is left under the type's own limit) and `available` (what can be sold right now:
`type_remaining`, but no more than what is left for the event; the same value as
the public `GET /ticket-types`). `event_remaining = event.capacity − sold −
active_holds` (`null` when there is no limit), and `total_available =
min(Σ type_remaining, event_remaining)`.

A hold is created for a limited time during checkout. After payment the hold
becomes sold tickets; when it expires, it is released.

### 5.6. Orders

Creates the order and its items, links the buyer to the attendees and enforces
the checkout deadline. Creating an order reserves the tickets in one transaction
for 10 minutes (`ORDER_RESERVATION_TTL_MINUTES`); when the time runs out a
background loop marks the order `EXPIRED` and releases the hold. A free
registration immediately creates a confirmed `PAID` order for `0.00 KZT` with
tickets — no payment details are needed. The organizer sees only registrations
from confirmed orders through `GET /api/v1/events/{event_id}/attendees`.

Main statuses:

```text
PENDING -> PAID
PENDING -> EXPIRED
PENDING -> CANCELLED
PAID -> PARTIALLY_REFUNDED
PAID/PARTIALLY_REFUNDED -> REFUNDED
```

### 5.7. Payments

One interface for the payment provider. The MVP uses a payment simulator
(see [Paid ticket sales](#paid-ticket-sales-demo-payment-simulation)), but the
domain logic works the same way it would with a real provider.

Key rules:

- a repeated payment result never issues tickets twice;
- every simulated payment is unique per order (unique index + row lock);
- payment confirmation runs in a single transaction;
- financial records are never physically deleted.

### 5.8. Tickets

After a successful payment one `ticket` row is created per attendee. The QR
code holds a signed payload (`v1.<public_id>.<HMAC-SHA256>` signed with
`TICKET_QR_SECRET`); the database stores only its SHA-256 hash. The payload is
deterministic, so a regenerated PDF always shows the same QR code.

Endpoints: `GET /tickets` (own tickets), `GET /tickets/{public_id}`,
`GET /tickets/{public_id}/pdf` and `/download` (PDF ticket), and
`GET /orders/{order_id}/tickets`.

### 5.9. Check-in

Checks the QR code, that the ticket belongs to the event and its status. A
double entry is prevented by locking the ticket row inside the transaction.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/events/{event_id}/check-in` | Scan a QR payload and admit (aliases: `/checkin`, `/tickets/check-in`) |
| `POST` | `/events/{event_id}/tickets/validate` | Check a QR payload without admitting (alias: `/validate`) |
| `POST` | `/events/{event_id}/tickets/{public_id}/check-in` | Manual check-in by ticket id |
| `POST` | `/events/{event_id}/tickets/{public_id}/reverse-check-in` | Undo a check-in (owner, `PLATFORM_ADMIN`, `EVENT_ADMIN`; not `CHECK_IN_STAFF`) |
| `GET` | `/events/{event_id}/tickets/search?q=` | Search attendees by name, email, ticket id or order number (non-empty `q`, paginated; alias: `/attendees/search`) |
| `GET` | `/events/{event_id}/tickets/stats` | Attendance counters |

All of them require owner, `PLATFORM_ADMIN` or assigned event staff access.

Every rejected admission has a machine-readable `error.details.admission_status`.
Error codes did not change, so older clients that handle `TICKET_NOT_VALID`
keep working. `POST .../tickets/validate` returns the same value in
`admission_status`.

| Case | HTTP | `error.code` | `admission_status` |
|---|---|---|---|
| Admitted | 200 | — | (response is the ticket) |
| Forged / unknown QR | 401 | `INVALID_TICKET_QR` | `INVALID_QR` |
| Campaign QR | 400 | `CAMPAIGN_QR_NOT_ADMISSION` | `CAMPAIGN_QR` |
| Ticket not found | 404 | `TICKET_NOT_FOUND` | `NOT_FOUND` |
| Ticket for another event | 404 | `TICKET_NOT_FOUND` | `WRONG_EVENT` |
| Scanned again | 409 | `TICKET_ALREADY_CHECKED_IN` | `ALREADY_CHECKED_IN` (+ `checked_in_at`) |
| Ticket cancelled | 409 | `TICKET_NOT_VALID` | `CANCELLED` |
| Ticket refunded | 409 | `TICKET_NOT_VALID` | `REFUNDED` |
| Reversing a ticket that is not checked in | 409 | `TICKET_NOT_CHECKED_IN` | `NOT_CHECKED_IN` |

The mobile client reads `admission_status` first and falls back to `error.code`.

### 5.10. Campaigns

Promo codes, campaign links, validity period, redemption limit and the ticket
types a campaign applies to.

Implemented in SCRUM-15 — see
[Promo-code campaigns and campaign QR codes](#promo-code-campaigns-and-campaign-qr-codes-scrum-15).

### 5.11. Notifications

Email verification, password reset, ticket delivery and event change notices.

- In development/test the log-only `DevEmailProvider` is used; nothing is sent.
- In production `EMAIL_PROVIDER=brevo` is required. The backend refuses to start
  with the dev provider, without `BREVO_API_KEY` or with an invalid sender email.
- Brevo is called directly with `httpx` (`POST /v3/smtp/email`), without a
  separate queue. The HTTP client is created once in the app lifespan.
- The order confirmation is stored in the `order_notifications` outbox, unique by
  `(order_id, kind)`, so a repeated confirm neither sends a second email nor
  issues new tickets.
- Temporary Brevo errors (429, 5xx, timeout) are retried with exponential
  backoff and respect `Retry-After`; permanent errors (400/401/403, invalid
  address) are marked `permanent:` and not retried.
- API keys, verification codes, reset tokens and email bodies are never logged.
- The password reset email has a button linking to
  `{PASSWORD_RESET_URL}?token=...&email=...` (by default
  `PASSWORD_RESET_URL = {PUBLIC_WEB_BASE_URL}/reset-password`). This can be an
  https page of the web client (the mobile app can intercept it as an App Link /
  Universal Link) or an app deep link such as `biletflow://reset-password`. The
  client reads `token` and `email` from the link and sends them with the new
  password to `POST /auth/reset-password`. When neither setting is set, the email
  shows the token instead.
- The email language comes from `users.locale`: `en` (default), `ru` or `kk`. It
  is set at registration (`locale` in `POST /auth/register`) and through
  `PATCH /users/me`; other values return `422`. Templates for verification,
  password reset, order confirmation, failed payment, event change/cancellation
  and refund exist in all three languages; missing strings fall back to English.
  Guest orders get English emails. Payload texts (for example a refund `notice`)
  and changed field names are still in English.
- In-app notifications: `GET /users/me/notifications` and
  `POST /users/me/notifications/{notification_id}/read`.

### 5.12. Support and audit

Support stores user cases (see
[Support cases](#support-cases-and-asynchronous-messages-scrum-16)). The audit
log records important actions: publishing an event, price changes, refunds,
check-ins, role changes and user suspensions.

---

## 6. API

All routes use the prefix:

```text
/api/v1
```

Endpoint groups (full list in the [endpoint reference](#endpoint-reference)):

| Module | Main endpoints |
|---|---|
| Auth | `/auth/register`, `/auth/verify-email`, `/auth/resend-verification`, `/auth/login`, `/auth/refresh`, `/auth/logout`, `/auth/logout-all`, `/auth/request-password-reset`, `/auth/reset-password` |
| Users | `/users/me`, `/users/me/notifications`, `/users/me/reports` |
| Organizers | `/organizers/me`, `/organizers/me/payout-account`, `/organizers/me/events`, `/organizers/me/analytics` |
| Events | `/events`, `/events/catalog`, `/events/assigned`, `/events/{event_id}` and its sub-resources (admins, attendees, inventory, analytics, timeline, refund policy, paid sales, images, calendar, reports) |
| Ticket types | `/events/{event_id}/ticket-types` |
| Campaigns | `/events/{event_id}/campaigns`, `/events/{event_id}/campaigns/analytics`, `/campaigns/validate`, `/campaigns/qr/{campaign_token}` |
| Orders and payments | `/orders`, `/orders/{order_id}`, `/orders/{order_id}/payments/simulate`, `/orders/payments/{payment_id}/simulate-result` |
| Refunds | `/orders/{order_id}/cancel`, `/orders/{order_id}/refund`, `/orders/{order_id}/refunds`, `/orders/{order_id}/refund-eligibility` |
| Tickets | `/tickets`, `/tickets/{public_id}`, `/tickets/{public_id}/pdf` |
| Check-in | `/events/{event_id}/check-in`, `/events/{event_id}/tickets/...` |
| Support | `/support/cases`, `/support/cases/{case_id}/messages` |
| Admin | `/admin/...` |
| Media | `/media/{key}` |

### 6.1. Success response

```json
{
  "data": {
    "id": "uuid",
    "status": "PAID"
  }
}
```

### 6.2. Error response

```json
{
  "error": {
    "code": "TICKET_SOLD_OUT",
    "message": "Selected ticket type is sold out",
    "details": {}
  }
}
```

Clients rely on the stable `error.code`; the `message` text may be translated or
changed. Validation errors use `VALIDATION_ERROR` with per-field details.

---

## 7. Authentication and authorization

### 7.1. Chosen approach for the MVP

> **Email + password, with mandatory email verification by a one-time code.**

OAuth2, SMS login and corporate SSO are not part of the MVP. They can be added
later without changing the user model.

### 7.2. Registration

```mermaid
sequenceDiagram
    participant C as Next.js or React Native
    participant A as FastAPI
    participant D as PostgreSQL
    participant E as Email

    C->>A: POST /auth/register
    A->>D: Create unverified user
    A->>D: Store hash of verification code
    A->>E: Send 6-digit code
    C->>A: POST /auth/verify-email
    A->>D: Validate code and expiration
    A->>D: Set email_verified_at
    A-->>C: Account verified
```

Passwords are hashed with a strong algorithm. Neither the plain password nor the
plain verification code is stored.

### 7.3. Login

1. The client sends email and password to `/auth/login`.
2. The backend finds the user and checks the password hash.
3. The backend checks the user status and email verification.
4. The backend creates a server-side refresh session.
5. The client receives a short-lived access token and a refresh token.

After `LOGIN_MAX_ATTEMPTS` failed attempts (default 5) the account is locked for
`LOGIN_LOCKOUT_MINUTES` (default 15).

Default lifetimes:

- access token — **15 minutes**;
- refresh token — **30 days**;
- email verification code — **10 minutes** (at most 5 attempts, resend cooldown 60 s, 5 codes per hour);
- password reset token — **30 minutes**.

### 7.4. Token storage on clients

Web (Next.js):

- the current app keeps access and refresh tokens in `localStorage` and calls the
  API through the Next.js `/api/v1` proxy; an `HttpOnly` cookie BFF is the
  recommended production option.

React Native:

- refresh token — in the device's secure storage (`expo-secure-store`);
- access token — in memory;
- requests send `Authorization: Bearer <token>`.

PostgreSQL stores a hash of the refresh token, not the token itself. Each refresh
replaces the old refresh token with a new one. Reusing an old token revokes the
whole session.

### 7.5. Authentication vs authorization

- **Authentication** answers "Who is the user?"
- **Authorization** answers "What is this user allowed to do?"

In BiletFlow authorization is based on roles and resource ownership:

| Role / condition | Permissions |
|---|---|
| Guest | View published public events |
| User | Buy tickets, see own orders and tickets |
| Event owner | Fully manage their own event |
| Event admin | Check in tickets and reverse check-ins for an assigned event |
| Check-in staff | Only check in tickets for an assigned event |
| Platform admin | Manage the platform, users and disputed operations |

All checks run on the backend. A hidden button in the frontend is not protection.

Example:

```python
if current_user.global_role == "PLATFORM_ADMIN":
    allow()
elif event.owner_user_id == current_user.id:
    allow()
elif has_event_permission(current_user.id, event.id, "EDIT_EVENT"):
    allow()
else:
    deny(status_code=403)
```

### 7.6. Auth tables

| Table | Purpose |
|---|---|
| `refresh_sessions` | Active devices, refresh token hash, expiry, revocation |
| `email_verification_tokens` | Email code hash, expiry and attempt count |
| `password_reset_tokens` | Reset token hash, expiry and whether it was used |

---

## 8. Key flows

### 8.1. Buying a ticket

```mermaid
sequenceDiagram
    participant C as Client
    participant A as FastAPI
    participant D as PostgreSQL
    participant P as Payment simulator

    C->>A: POST /orders
    A->>D: Lock inventory and create hold
    A-->>C: Order PENDING
    C->>A: POST /orders/{id}/payments/simulate
    A->>P: Create simulated payment
    C->>A: POST /orders/payments/{id}/simulate-result
    A->>D: Mark payment and order PAID
    A->>D: Consume hold and create tickets
    A-->>C: Order PAID + tickets
```

Everything after the payment is confirmed runs in one transaction. Processing the
same result again returns success but does not create new tickets.

### 8.2. Check-in

1. The mobile app scans the QR code.
2. It sends the payload and `event_id` to FastAPI.
3. The backend verifies the signature, hashes the payload and finds the ticket.
4. PostgreSQL locks the ticket row with `SELECT ... FOR UPDATE`.
5. The backend checks the event and the `VALID` status.
6. The status becomes `CHECKED_IN` and a `checkin_record` is created.
7. A repeated scan returns a conflict with the time of the first entry.

---

## 9. PostgreSQL

PostgreSQL is the single source of truth for users, orders, stock, payments and
tickets.

Rules:

- UUIDs for business entities;
- `TIMESTAMPTZ` for dates and times;
- money as `NUMERIC(12,2)`, never `float`;
- migrations live in Git (`sql/`);
- financial, ticket and audit records are never physically deleted;
- order creation, inventory changes and check-in use transactions;
- foreign keys and unique constraints are enforced by the database, not only in Python;
- catalog, order and check-in queries use the indexes from the SQL schema.

### 9.1. Overselling protection

When creating a hold the backend:

1. starts a transaction;
2. locks the needed `ticket_types` rows;
3. counts paid tickets and active, unexpired holds;
4. compares the result with the capacity;
5. creates the hold or returns `TICKET_SOLD_OUT`;
6. commits.

A plain stock check without a lock is not enough: two parallel requests could
both see the last ticket.

---

## 10. Security

- HTTPS everywhere except local development.
- Passwords are hashed and never logged.
- Secrets are environment variables; `.env` is not committed.
- Login, verification and password reset are rate-limited.
- All input is validated by Pydantic schemas.
- CORS allows only the web and mobile origins of the environment.
- An object id from the URL is always checked together with the user's permissions.
- Sensitive operations are written to the audit log.
- Ticket QR payloads are HMAC-signed; only their hash is stored.
- Uploaded images are decoded and re-encoded, which strips metadata such as GPS.

---

## 11. Configuration

Settings are read from the environment / `.env` (`app/core/config.py`). See
`.env.example` for the full list. The most important ones:

| Variable | Default | Purpose |
|---|---|---|
| `APP_ENV` | `development` | `production` enables strict checks (Brevo required) |
| `DATABASE_URL` | `postgresql+asyncpg://biletflow:biletflow@localhost:5432/biletflow` | PostgreSQL connection |
| `REDIS_URL` | empty | Redis; empty uses the in-process store |
| `CORS_ORIGINS` | empty | Comma-separated allowed origins, e.g. `http://localhost:3000,http://localhost:8081` |
| `JWT_ACCESS_SECRET`, `JWT_REFRESH_SECRET` | — | Required token signing secrets |
| `TICKET_QR_SECRET` | — | Secret for signing ticket QR payloads |
| `ACCESS_TOKEN_TTL_MINUTES` / `REFRESH_TOKEN_TTL_DAYS` | 15 / 30 | Token lifetimes |
| `EMAIL_CODE_TTL_MINUTES`, `EMAIL_CODE_MAX_ATTEMPTS`, `EMAIL_CODE_RESEND_COOLDOWN_SECONDS`, `EMAIL_CODE_MAX_PER_HOUR` | 10, 5, 60, 5 | Verification code limits |
| `LOGIN_MAX_ATTEMPTS` / `LOGIN_LOCKOUT_MINUTES` | 5 / 15 | Login lockout |
| `PASSWORD_RESET_TTL_MINUTES` / `PASSWORD_RESET_URL` | 30 / empty | Reset token lifetime and link target |
| `EMAIL_PROVIDER` | `dev` | `dev` (log only) or `brevo` |
| `EMAIL_FROM`, `BREVO_API_KEY`, `BREVO_SENDER_EMAIL`, `BREVO_SENDER_NAME`, `BREVO_TIMEOUT_SECONDS` | — | Brevo email |
| `PUBLIC_BASE_URL` | empty | Absolute API URL used in media links |
| `PUBLIC_WEB_BASE_URL` | empty | Web app URL for links in emails and calendar files |
| `ORDER_RESERVATION_TTL_MINUTES` | 10 | How long a pending order holds tickets |
| `RESERVATION_CLEANUP_ENABLED` / `RESERVATION_CLEANUP_INTERVAL_SECONDS` | true / 60 | Background loop for expired orders and completed events |
| `NOTIFICATION_RETRY_INTERVAL_SECONDS`, `NOTIFICATION_MAX_ATTEMPTS`, `NOTIFICATION_RETRY_BASE_SECONDS`, `NOTIFICATION_RETRY_MAX_SECONDS` | 60, 5, 30, 3600 | Email retries |
| `EVENT_CANCELLATION_BATCH_SIZE` | 100 | Orders processed per batch when an event is cancelled |
| `DEMO_SIMULATIONS_ENABLED` | true | Demo self-verification and payout approval |
| `MEDIA_ROOT` / `MEDIA_MAX_UPLOAD_BYTES` | `media` / 5 MB | Image storage |
| `APPLY_SCHEMA_ON_STARTUP` | true | Apply migrations when the app starts |

For Brevo the sender email must be verified in the Brevo account (Sender
Authentication). `BREVO_SENDER_EMAIL`/`BREVO_SENDER_NAME` take priority; when
they are empty the address from `EMAIL_FROM` is used. Automated tests always use
`DevEmailProvider` and make no network calls.

Real secret values must never be committed.

---

## 12. Testing

The suite in `tests/` covers auth, password reset, events, event staff,
organizers, ticket types and orders, paid sales, tickets and check-in workflows,
campaigns, refunds, support, admin and analytics, reports / settings / media,
privacy and payout review, email provider and localization, error format,
migrations, and the contract the frontend relies on. `TEST_REPORT.md` has an
earlier test run report.

What the tests check:

- **Unit:** price and discount calculation, allowed status transitions,
  permissions, token creation and verification, refund rules.
- **Integration:** repository operations, transactional ticket reservation,
  repeated payment results, double check-in protection, refresh session
  revocation.
- **End-to-end flows:** register → verify email → login; create event → publish;
  buy → pay → get a QR ticket; scan QR → scan again; cancel / refund.

---

## 13. Logging and health checks

Logs contain the request ID, endpoint, HTTP status, duration and user ID when
the user is signed in. Passwords, access/refresh tokens, QR payloads and full
payment details are never logged.

```text
GET /health/live   — the FastAPI process is running
GET /health/ready  — PostgreSQL (and Redis, when configured) are reachable
```

---

## 14. Implementation order (as planned and completed)

1. FastAPI skeleton, configuration and PostgreSQL connection.
2. Migrations and base user models.
3. Email/password auth and refresh sessions.
4. Roles, ownership and permissions.
5. Events and ticket types.
6. Inventory holds and orders.
7. Payment simulator and idempotent result handling.
8. Tickets and QR payloads.
9. Check-in.
10. Campaigns, notifications, support and audit.
11. Refunds, administration, analytics, reports and platform settings.
12. Integration tests for the critical flows.

---

## 15. Decisions for the Academic MVP

| Question | Decision |
|---|---|
| Backend | FastAPI |
| Web | Next.js |
| Mobile | React Native (Expo) |
| Database | PostgreSQL |
| Architecture | Modular monolith |
| API | REST `/api/v1` |
| Login | Email + password |
| Verification | One-time code by email |
| Session | Access token + rotating refresh token |
| Permissions | Global role + event ownership + event staff roles |
| Payment | Simulator behind an adapter |
| Ticket | One row per attendee, HMAC-signed QR payload |
| SMS | Not in the MVP |
| OAuth2 login | After the MVP if needed |
| SSO | Not needed for this product |

---

## 16. Organizer profile and Event Admin

### 16.1. Organizer profile

All endpoints require a verified email (`Depends(require_verified_user)`) and
work only with the current JWT user's profile. `user_id`,
`verification_status`, `created_at` and `updated_at` cannot be set in the body.

```http
POST   /api/v1/organizers/me                  # create the profile (201)
GET    /api/v1/organizers/me                  # own profile (404 ORGANIZER_PROFILE_NOT_FOUND if missing)
PATCH  /api/v1/organizers/me                  # partial update
GET    /api/v1/organizers/me/payout-account   # default (or newest) payout account, masked
PUT    /api/v1/organizers/me/payout-account   # demo payout account
GET    /api/v1/organizers/me/events           # own events
GET    /api/v1/organizers/me/analytics        # totals across own events
```

Example:

```json
{
  "display_name": "BiletFlow Events",
  "legal_name": "BiletFlow Events LLP",
  "contact_email": "organizer@example.com",
  "contact_phone": "+77001234567",
  "terms_accepted": true
}
```

- One user has one `OrganizerProfile`; creating it again →
  `409 ORGANIZER_PROFILE_ALREADY_EXISTS`.
- `verification_status` starts as `NOT_SUBMITTED` and is changed only by the backend
  (a Platform Admin, or the demo `POST /organizers/me/verification/simulate`).
- The payout account is demo data: `status` is always `PENDING` on creation and
  `external_account_ref` is masked in responses (`****-001`). Card numbers / CVV
  are neither accepted nor stored.
- With `is_default=true` the organizer's previous default account is unset in the
  same transaction.

### 16.2. Event Admin

Event Admin is an **event-scoped** role (`event_staff.role = EVENT_ADMIN`), not a
global one. Assigning it does not change `users.global_role`. Assignments are not
put into the JWT: permissions are checked against PostgreSQL on every request, so
removing an assignment takes effect immediately.

```http
GET    /api/v1/events/assigned                        # events where current_user has check-in access: owner or EVENT_ADMIN/CHECK_IN_STAFF (limit/offset paging)
GET    /api/v1/events/{event_id}/admins               # event admins of the event
POST   /api/v1/events/{event_id}/admins               # assign (body: {"email": "..."})
DELETE /api/v1/events/{event_id}/admins/{user_id}     # remove the assignment (204)
```

Each item of `/events/assigned` includes the caller-specific
`can_reverse_check_in` flag (true for the owner, `PLATFORM_ADMIN` and
`EVENT_ADMIN`; false for `CHECK_IN_STAFF`).

Example:

```json
{
  "email": "event-admin@example.com"
}
```

Assignment rules:

- only the event owner or a `PLATFORM_ADMIN` can assign/remove;
- the target user must exist, be `ACTIVE` and have a verified email;
- the owner cannot be made admin of their own event (`409 EVENT_OWNER_CANNOT_BE_ADMIN`);
- assigning the same `(event_id, user_id)` again → `409 EVENT_ADMIN_ALREADY_ASSIGNED`;
- an Event Admin gets only `{"view_event": true, "check_in_tickets": true}` — they
  cannot change the owner, manage staff or see other events.

An Event Admin sees only events with an `event_staff(event_id, user_id)` row. The
filtering is an SQL join `events ⋈ event_staff`, not Python code.

### 16.3. PLATFORM_ADMIN vs EVENT_ADMIN

| | PLATFORM_ADMIN | EVENT_ADMIN |
|---|---|---|
| Scope | Whole platform | One specific event |
| Stored in | `users.global_role` | a row in `event_staff` |
| Assigned by | server command (`make admin`) | event owner or `PLATFORM_ADMIN` |
| Sees events | all | only assigned ones |
| Manages event staff | yes | no |
| Event check-in | yes | yes (while the assignment is active) |

### 16.4. Event-scoped authorization

- `require_event_owner(event_id)` — event owner or `PLATFORM_ADMIN` (event management, staff).
- `require_event_staff_role(*roles)` — only event staff with the given role.
- `require_event_checkin_access(event_id)` — owner, `PLATFORM_ADMIN` or an active
  `EVENT_ADMIN`/`CHECK_IN_STAFF` of the event. Used by the check-in API.

Denials are recorded in `audit_logs` as `event.staff_access_denied`.

### 16.5. Audit

| Action | `action` |
|---|---|
| Profile created | `organizer.profile_created` |
| Profile updated | `organizer.profile_updated` |
| Payout account changed | `organizer.payout_account_updated` |
| Event Admin assigned | `event.admin_assigned` |
| Event Admin removed | `event.admin_removed` |
| Event access denied | `event.staff_access_denied` |

Password hashes, JWTs, refresh tokens and full payout references never go into
the audit log.

### 16.6. Running tests locally

```bash
cp .env.example .env            # fill in JWT_ACCESS_SECRET / JWT_REFRESH_SECRET
pip install -e ".[dev]"         # pytest, aiosqlite, ruff, mypy
make lint                       # ruff check app tests
make typecheck                  # mypy
make test                       # pytest
make migrate                    # apply SQL migrations to DATABASE_URL
```

SQL migrations (`sql/migrate_*.sql`) are versioned: each runs exactly once, and
applied versions are stored in the `schema_migrations` table. Databases created
before tracking was introduced are detected by their existing tables and get the
base versions without running them again.

By default the tests use in-memory SQLite (the project's test fallback). To run
against a separate PostgreSQL test database (including the parallel reservation
test and migration scenarios):

```bash
TEST_DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/test_db make test
```

CI (`.github/workflows/ci.yml`) runs lint, typecheck, the tests on SQLite and the
full suite on PostgreSQL 17. `deploy.yml` deploys over SSH after CI passes on
`main` (only when `CD_ENABLED=true`, and only if `main` has not moved since the
CI run).

## Paid ticket sales (demo payment simulation)

Paid sales are activated **per event**. The event owner can inspect the checklist,
accept the terms, simulate the activation fee, and activate sales only after the
organizer profile is verified and an ACTIVE payout account exists.

- `GET /api/v1/events/{event_id}/paid-sales/activation`
- `POST /api/v1/events/{event_id}/paid-sales/terms` with `{"accepted": true}`
- `POST /api/v1/events/{event_id}/paid-sales/activation-fee` (internal demo only)
- `POST /api/v1/events/{event_id}/paid-sales/activate`

The activation-fee endpoint records a **simulation**, not a real charge. The
demo activation fee is 1,000 KZT.

Paid order creation is rejected until that event is activated. The backend
calculates a 3% processing fee (rounded to two decimal places) and stores the
result in the order. The current demo checkout supports KZT ticket types.

1. Create a pending order with `POST /api/v1/orders`.
2. Start the internal simulation with
   `POST /api/v1/orders/{order_id}/payments/simulate`.
3. Resolve the demo payment with
   `POST /api/v1/orders/payments/{payment_id}/simulate-result` and
   `{"outcome": "success"}`, `{"outcome": "failed"}`, or
   `{"outcome": "abandoned"}`.

Every simulated payment response is marked `is_simulated: true` and carries a
demo-only notice. No real payment provider is contacted. A successful result
confirms the order and issues tickets; failed/abandoned results cancel the
pending order and release its inventory holds. The unique simulated-payment
index and row locking make repeated starts/callbacks idempotent. The legacy
`/confirm` and `/checkout` endpoints no longer allow clients to self-confirm
paid orders; use the payment confirmation flow instead.

Apply the new `migrate_006_paid_sales.sql` migration before using these
endpoints (it is included in the default startup migration list).


## Promo-code campaigns and campaign QR codes (SCRUM-15)

A campaign belongs to one event and carries a promo code, a discount, an
optional validity window, an optional redemption limit and an optional list of
ticket types. Organizer endpoints require the event owner (or a
`PLATFORM_ADMIN`) through the shared `require_event_owner` dependency.

### Endpoints

| Method | Path | Access | Purpose |
|---|---|---|---|
| `POST` | `/api/v1/events/{event_id}/campaigns` | owner | Create a campaign |
| `GET` | `/api/v1/events/{event_id}/campaigns` | owner | List campaigns |
| `GET` | `/api/v1/events/{event_id}/campaigns/{campaign_id}` | owner | Campaign details |
| `PATCH` | `/api/v1/events/{event_id}/campaigns/{campaign_id}` | owner | Update / disable (`{"is_active": false}`) |
| `GET` | `/api/v1/events/{event_id}/campaigns/{campaign_id}/analytics` | owner | Performance of one campaign |
| `GET` | `/api/v1/events/{event_id}/campaigns/analytics` | owner | Performance of all event campaigns |
| `GET` | `/api/v1/events/{event_id}/campaigns/{campaign_id}/qr.png` | owner | PNG of the campaign QR link |
| `POST` | `/api/v1/campaigns/validate` | signed-in user | Validate a code and preview prices (never consumes) |
| `GET` | `/api/v1/campaigns/qr/{campaign_token}` | public | Resolve a scanned campaign QR (never consumes) |
| `POST` | `/api/v1/orders` | signed-in user | Checkout; accepts optional `promo_code` **or** `campaign_token` |

Create request:

```json
{
  "name": "Spring promo",
  "code": "SPRING10",
  "discount_type": "PERCENT",
  "discount_value": "10",
  "starts_at": "2026-12-01T00:00:00+05:00",
  "expires_at": "2026-12-31T23:59:59+05:00",
  "redemption_limit": 100,
  "ticket_type_ids": ["<ticket-type-uuid>"],
  "is_active": true
}
```

Validation: `PERCENT` is `0 < v <= 100`; `FIXED` is a positive KZT amount with
at most 2 decimals; `expires_at > starts_at`; timestamps need a timezone
offset (stored in UTC); `redemption_limit >= 1` when given; ticket types must
belong to the campaign's event; codes are 3–40 chars `[A-Za-z0-9_-]`, stored
upper-case and unique per event (case-insensitive). An empty `ticket_type_ids`
means the campaign applies to all ticket types. The discount cannot change once
the code has been used (`409 CAMPAIGN_IN_USE`), and the limit cannot drop below
current usage (`409 REDEMPTION_LIMIT_BELOW_USAGE`). Campaigns are never
deleted; disable them so attribution stays intact.

Checkout with a promotion:

```json
POST /api/v1/orders
{"event_id": "...", "items": [{"ticket_type_id": "...", "quantity": 2}], "promo_code": "spring10"}
```

Response fields added to every order: `campaign_id` and `promo_code` (the
historical code snapshot); `subtotal_amount`, `discount_amount`, `fee_amount`
and `total_amount` already existed.

### Promo errors

All use the standard error envelope (`{"error": {"code", "message", "details"}}`).

| HTTP | Code | Meaning |
|---|---|---|
| 404 | `PROMO_NOT_FOUND` | No such code for this event / unknown token |
| 409 | `PROMO_DISABLED` | Campaign is disabled |
| 409 | `PROMO_NOT_STARTED` | Before `starts_at` |
| 409 | `PROMO_EXPIRED` | At/after `expires_at` |
| 409 | `PROMO_LIMIT_REACHED` | Redemption limit exhausted |
| 409 | `PROMO_NOT_APPLICABLE` | No eligible ticket types in the order, eligible items are free, token from another event, or `FIXED` on a non-KZT order |
| 409 | `PROMO_ALREADY_RESERVED` | The same customer already has a pending checkout with this code; `details.order_id` points to it |
| 404 | `CAMPAIGN_NOT_FOUND` | QR token unknown, malformed, or its event is not published |

### Discount calculation (server-side only)

The backend recalculates every amount at checkout. Monetary fields sent by a
client are ignored. All maths uses `Decimal` and `NUMERIC(12,2)`, never floats.

- **Eligibility:** only units of eligible ticket types are discounted. Other
  units in the same order pay full price (mixed orders are allowed).
- **PERCENT:** each eligible unit gets `round_half_up(unit_price × pct / 100, 0.01)`.
  The order discount is the sum of the unit discounts.
- **FIXED (KZT):** one amount per order, capped at the eligible subtotal. It is
  split across eligible units in proportion to price, in whole tiyn. Leftover
  tiyn go to the first units in ticket-type-id order, so the split is
  deterministic. Totals can never go negative.
- **Fee:** the 3% processing fee is charged on the discounted amount.
  `total = subtotal − discount + fee` (enforced by the `orders_total_formula`
  CHECK).
- **No stacking:** one promotion per order. Sending both `promo_code` and
  `campaign_token` returns `422`.
- A discount that brings the payable amount to `0.00` confirms the order
  immediately (no payment step). Events with priced tickets still need paid
  sales to be activated.

Example: 2 × 25.00 KZT with 10% → subtotal 50.00, discount 5.00, fee 1.35,
total 46.35.

### Redemption lifecycle and concurrency

`promo_redemptions.status` (one row per order) moves through:

```text
checkout (paid)  -> RESERVED --payment succeeded--> REDEEMED
checkout (free)  -> REDEEMED
RESERVED --payment failed/abandoned, reservation expired, order cancelled--> RELEASED
REDEEMED --order cancelled/refunded--------------------------------------> RELEASED
```

- `campaigns.redemption_count` = RESERVED + REDEEMED. Checkout takes capacity
  with a conditional `UPDATE … SET redemption_count = redemption_count + 1
  WHERE redemption_count < max_redemptions`. Under concurrency PostgreSQL
  re-checks that predicate on the latest row version, and the CHECK constraint
  `campaigns_redemption_count_valid` keeps the count at or below the limit.
  Checkouts for one event also serialize on the event row lock.
- Releases are guarded by the status predicate, so retries and concurrent
  cleanup never release twice or push the counter negative.
- A pending paid checkout holds capacity until its reservation TTL
  (`ORDER_RESERVATION_TTL_MINUTES`). A failed, abandoned or expired checkout
  returns that capacity. Previewing (`/campaigns/validate`) and QR resolution
  are read-only.
- A partial unique index allows one RESERVED redemption per customer per
  campaign. A repeated checkout gets `PROMO_ALREADY_RESERVED` and cannot drain
  the limit.
- A reservation is honoured at payment time even if the campaign has expired
  or been disabled since checkout (the price was locked when the order was
  created).

### Campaign QR codes (separate from admission QR)

- A campaign QR is a **promotional link, not a ticket**. It encodes
  `{PUBLIC_WEB_BASE_URL}/campaigns/{campaign_token}`. The token is a random
  UUIDv4 (122 bits from the OS CSPRNG): opaque, not sequential, and it carries
  no user data or secrets.
- Admission QR codes are HMAC-signed `v1.<ticket public_id>.<signature>`
  payloads. The check-in and validate endpoints explicitly reject campaign
  tokens, bare or as a URL, with `400 CAMPAIGN_QR_NOT_ADMISSION`. Any other
  URL or random string gets `401 INVALID_TICKET_QR`. Ticket payloads do not
  resolve as campaigns.
- `PUBLIC_WEB_BASE_URL` has no default, so no production domain is invented.
  While it is empty, responses include `campaign_token` and a relative
  `qr_path`, `qr_url` is `null`, and `qr.png` returns
  `409 CAMPAIGN_LINK_NOT_CONFIGURED`. Production requires an `https://` origin.

**Frontend integration contract:**

1. The web app serves the route `/campaigns/:token`.
2. That page calls `GET /api/v1/campaigns/qr/{token}`. The response has the
   event summary, the promotion (name, discount type and value, window,
   eligible ticket types) and `checkout: {event_id, campaign_token}`. It
   exposes no code, counts or buyer data.
3. Optionally preview prices with `POST /api/v1/campaigns/validate` using
   `campaign_token`.
4. Check out with `POST /api/v1/orders`, passing `campaign_token`. The server
   validates the promotion again and prices the order itself.

### Analytics definitions

`GET …/campaigns/{campaign_id}/analytics` is computed from the database at
request time:

| Field | Definition |
|---|---|
| `redemption_count` | Capacity in use now (RESERVED + REDEEMED) |
| `reserved_redemptions` | Pending paid checkouts |
| `successful_redemptions` | REDEEMED (paid/free-confirmed, not reversed) |
| `released_redemptions` | Failed, expired, cancelled or refunded usages |
| `discount_total` | Sum of discounts on REDEEMED rows |
| `attributed_order_count` / `attributed_revenue` | **Net**: orders currently `PAID`/`CONFIRMED`, sum of `total_amount` actually charged |
| `gross_attributed_order_count` / `gross_attributed_revenue` | Every order that was ever paid/confirmed (`paid_at` set), including ones later reversed |
| `reversed_order_count` / `reversed_revenue` | Paid orders later cancelled or refunded (gross − net) |

Refunds and cancellations lower net revenue and successful redemptions. Gross
figures keep the historical sale. Pending, expired and failed checkouts never
count as sales. Revenue includes the processing fee because it is the amount
the customer paid.

### Migration

`sql/migrate_007_campaigns.sql` (in the default `SCHEMA_FILES` list, applied
on startup or with `make migrate`) adds:
- `campaigns.redemption_count` and its CHECK constraint
- `promo_redemptions.status`, `user_id`, `released_at` and `updated_at`
- `orders.promo_code_snapshot`
- indexes for the reservation uniqueness, campaign/status lookups and orders
  by campaign

All changes are additive with defaults. Existing rows are backfilled and
nothing is dropped.


## Cancellations, refunds, notifications and audit log (SCRUM-14)

All refunds are **simulated** (`is_simulated: true`): no payment provider is
contacted and no real money moves. Only full refunds are supported.

### Endpoints

| Method | Path | Access | Purpose |
|---|---|---|---|
| `GET` | `/api/v1/events/{event_id}/refund-policy` | public (visible events) | Read the refund policy |
| `PUT` | `/api/v1/events/{event_id}/refund-policy` | owner | Set `refunds_enabled`, `refund_deadline_hours`, `policy_text` |
| `POST` | `/api/v1/orders/{order_id}/cancel` | buyer | Cancel a pending or free order, or refund a paid one (returns `OrderResponse`) |
| `POST` | `/api/v1/orders/{order_id}/refund` | buyer | Explicit refund of a paid order (returns `RefundResultResponse`) |
| `GET` | `/api/v1/orders/{order_id}/refunds` | buyer, owner, admin | Refund records for the order |
| `GET` | `/api/v1/orders/{order_id}/refund-eligibility` | buyer | Read-only preview: cancellable or refundable, reason, amount, deadline |
| `POST` | `/api/v1/events/{event_id}/cancel` | owner | Cancel the event (optional body `{"reason": "..."}`); safe to repeat |
| `GET` | `/api/v1/events/{event_id}/cancellation` | owner | Cancellation progress and totals |
| `GET` | `/api/v1/events/{event_id}/audit-log?limit=&offset=&action=` | owner | Read-only audit trail for the event |

`PLATFORM_ADMIN` may act wherever the owner or buyer can.

```json
PUT /api/v1/events/{event_id}/refund-policy
{"refunds_enabled": true, "refund_deadline_hours": 24, "policy_text": "Full refund until 24h before start"}

POST /api/v1/orders/{order_id}/refund  ->  200
{"order_id": "...", "order_status": "REFUNDED", "replayed": false,
 "refund": {"id": "...", "amount": "23.18", "currency": "KZT", "status": "SUCCEEDED",
            "kind": "CUSTOMER_REQUEST", "is_simulated": true, "...": "..."},
 "notice": "DEMO ONLY — refunds are simulated; no real money is moved."}
```

### Refund eligibility rules (enforced server-side)

**Customer requests** (`/cancel`, `/refund`) are allowed only for the **buyer**
(or a platform admin) and only when:

- the order is confirmed;
- no ticket has been checked in (`409 TICKET_ALREADY_CHECKED_IN`);
- the event has not started (`409 EVENT_ALREADY_STARTED`).

A **paid** order must also meet the event policy:

- `refunds_enabled` is true (`409 REFUNDS_DISABLED`);
- the request is before `starts_at − refund_deadline_hours` (`409 REFUND_DEADLINE_PASSED`).

Rejected requests are recorded in the audit log as `refund.denied`.

- New and existing events default to **refunds disabled**.
- A free registration can be cancelled until the event starts. It never creates
  a monetary refund, and asking it for a refund returns `409 ORDER_NOT_PAID`.
- A pending, unpaid checkout can always be cancelled. Its hold and promo
  reservation are released and the pending simulated payment is marked
  `CANCELLED`.
- **The event owner may cancel or refund individual orders** of their own event
  through `/orders/{id}/cancel` and `/refund` (SRS 4.9). A paid order gets a full
  refund (`kind = ORGANIZER_REQUEST`) regardless of `refunds_enabled`, the
  deadline or the start time; a free registration is cancelled without a
  refund; checked-in orders are refused. Other organizers get `403`.

**Organizer event cancellation** follows its own policy. Every paid order gets
a full refund regardless of `refunds_enabled` or the deadline. Free
registrations are cancelled without a refund, and pending checkouts are
cancelled. Refund records carry `kind`: `CUSTOMER_REQUEST`,
`EVENT_CANCELLATION` or `ORGANIZER_REQUEST`.

### Lifecycle transitions

```text
Paid refund:      Order PAID -> REFUNDED | Payment SUCCEEDED -> REFUNDED
                  Refund PENDING -> SUCCEEDED (simulated, same transaction)
                  Tickets VALID -> REFUNDED
Free cancel:      Order PAID/CONFIRMED -> CANCELLED | Tickets VALID -> CANCELLED
Pending cancel:   Order PENDING -> CANCELLED | Payment PENDING -> CANCELLED | holds RELEASED
Event cancelled:  Event -> CANCELLED (cancelled_at, cancellation_reason), then each order as above
```

Original financial records stay unchanged, and refunds are separate rows:

- payment `amount` and `paid_at`;
- order `subtotal_amount`, `discount_amount`, `total_amount` and `paid_at`.

The refund amount is the payment actually charged, so a promo order refunds
its discounted total including the fee. Every transition also releases the
promo redemption (SCRUM-15) and frees inventory.

Refunded and cancelled tickets fail both `/tickets/validate`
(`valid: false, reason: TICKET_REFUNDED | TICKET_CANCELLED`) and `/check-in`
(`409 TICKET_NOT_VALID`). This is the existing admission check; no second
validation path was added.

### Idempotency and concurrency

- Every refund or cancellation locks the **order row, then its payment**. The
  payment simulation uses the same order (changed from payment-then-order), so
  the flows serialize without deadlocks.
- A refund is only created from `PAID` + `SUCCEEDED`. A partial unique index
  `uq_refunds_active_per_payment` allows **one non-failed refund per payment**
  at the database level.
- Repeating `/refund` or `/cancel` returns `200` with the stored result and
  `replayed: true` (for `/refund`). Nothing is processed again and no second
  email is queued. Concurrent requests serialize on the row lock: one does the
  work and the rest replay.
- No network call happens while business rows are locked. Emails are queued in
  the outbox and sent after commit.

### Event cancellation processing

1. The event row is locked, set to `CANCELLED` (with an audit entry) and
   committed, so new checkouts and payments are rejected (`EVENT_CANCELLED`).
2. Active orders are closed in committed batches of
   `EVENT_CANCELLATION_BATCH_SIZE`, using `FOR UPDATE SKIP LOCKED`. Progress is
   the orders themselves: an order leaves PENDING/PAID/CONFIRMED exactly once.
3. If the process dies mid-way, calling `POST /events/{id}/cancel` again, or
   the background sweep (every `RESERVATION_CLEANUP_INTERVAL_SECONDS`),
   resumes with the remaining orders. Refunds and notifications are never
   duplicated.
4. `GET /events/{id}/cancellation` reports `orders_remaining`,
   `orders_refunded`, `orders_cancelled`, `event_cancellation_refund_total` and
   `complete`.

Campaign attribution (`orders.campaign_id`, `promo_code_snapshot`) is kept, and
campaign analytics count these orders as reversed.

### Notifications

Notifications use the existing transactional outbox (`order_notifications`)
and the existing provider (Brevo in production, the log-only dev provider
otherwise). Rows are written in the **same transaction** as the business
change and delivered after commit; the retry worker uses exponential backoff
and treats permanent errors as final. Uniqueness on
`(order_id, kind, dedupe_key)` makes enqueueing idempotent.

| Kind | Trigger | Dedupe | Context in the email |
|---|---|---|---|
| `PAYMENT_FAILED` | simulated payment `failed` (not `abandoned`) | per payment | event, order #, amount, status |
| `EVENT_UPDATED` | owner changes title, start/end time, timezone, venue name/address or city on a published event | per update × order | event, order #, changed fields, timestamp |
| `EVENT_CANCELLED` | event cancellation (confirmed orders) | per order | event, order #, reason, refund amount (if any), timestamp |
| `REFUND_COMPLETED` | customer refund | per order | event, order #, refund amount, timestamp, demo notice |

Event-cancellation refunds are announced inside the `EVENT_CANCELLED` email,
so buyers get one email, not two. Emails contain no payment details, tokens or
other customers' data, and all user text is HTML-escaped.

### Audit log

`audit_logs` gives each entry these fields:

- `id`, `timestamp` (`created_at`)
- `actor_id` (`actor_user_id`), `actor_type` (`CUSTOMER`, `ORGANIZER`,
  `PLATFORM_ADMIN`, `USER` or `SYSTEM`)
- `action`, `entity_type`, `entity_id`, `event_id`
- `metadata`, `before`, `after`
- `request_id` (the `X-Request-ID` correlation id)

Entries are written in the business transaction, so they commit or roll back
together with it.

Recorded actions include `event.updated`, `event.cancelled`,
`event.refund_policy_updated`, `order.cancelled`, `order.refunded`,
`refund.requested`, `refund.completed`, `refund.denied`,
`payment.state_changed` and `campaign.created/updated`.

Keys that look like credentials (`password`, `token`, `secret`, `api_key`,
`card`, …) are stripped, and no emails or payment secrets are written.

**Append-only through ordinary interfaces:**

- **API:** only `GET` exists; `POST`, `PUT`, `PATCH` and `DELETE` return
  `405`. Owners see only their own events.
- **ORM:** a `before_update`/`before_delete` listener raises.
- **PostgreSQL:** the trigger `trg_audit_logs_append_only` rejects `UPDATE`,
  `DELETE` and `TRUNCATE`. The one exception is the FK cascade that nulls
  `actor_user_id`/`event_id` when a user or draft event is deleted.

This is **not cryptographic immutability**: a database superuser or the table
owner can disable the trigger. For stronger guarantees, run the API with a
role that does not own the table.

### Migration

`sql/migrate_008_refunds_audit.sql` is in the default `SCHEMA_FILES` and is
applied on startup or with `make migrate`. It adds:

- event policy and cancellation columns;
- refund `kind`, `currency`, `is_simulated`, `completed_at` and the unique
  refund index;
- outbox `dedupe_key` and `payload` (replacing the `(order_id, kind)` unique
  constraint with `(order_id, kind, dedupe_key)`);
- audit `actor_type`, `metadata`, `request_id`, an event index and the
  append-only trigger.

All changes are additive with defaults. Existing audit rows with an actor are
backfilled to `actor_type = 'USER'`.

### Testing simulated refunds manually

1. Create and publish an event with a paid KZT ticket type and activate paid
   sales (see *Paid ticket sales* above).
2. `PUT /events/{id}/refund-policy` with `{"refunds_enabled": true}`.
3. Buy: `POST /orders`, then `POST /orders/{id}/payments/simulate`, then
   `POST /orders/payments/{payment_id}/simulate-result` with `{"outcome": "success"}`.
4. `GET /orders/{id}/refund-eligibility`, then `POST /orders/{id}/refund`.
   Repeat the call and expect `replayed: true`.
5. Scan the old ticket QR at `/events/{id}/check-in` and expect `409 TICKET_NOT_VALID`.
6. `GET /events/{id}/audit-log` to see the trail. With `EMAIL_PROVIDER=dev`,
   emails appear in the API logs.
7. For event cancellation: `POST /events/{id}/cancel` with a reason, then
   `GET /events/{id}/cancellation`.

Automated: `make test` (SQLite) or, for the locking, concurrency and trigger
tests, `TEST_DATABASE_URL=postgresql+asyncpg://… make test`.

## Support cases and asynchronous messages (SCRUM-16)

REST + polling (no WebSockets). Lifecycle history is written to the existing
append-only `audit_logs`; messages live in `support_messages`; notifications
are in-app rows in the existing `notifications` table.

### Endpoints

| Method | Path | Who | Purpose |
|---|---|---|---|
| `POST` | `/api/v1/support/cases` | any active user | Open a case (`subject`, `category`, `message`, optional `ticket_id` / `order_id` / `event_id`, optional `case_type`) |
| `GET` | `/api/v1/support/cases` | any active user | Accessible cases only; `scope=mine\|event\|all` (all: Platform Admins), filters `status` (repeatable), `category`, `case_type`, `event_id`, `escalated`, `assigned_to_me`, `role=requester\|handler`, `q`; `sort`, `limit`, `offset` |
| `GET` | `/api/v1/support/cases/{id}` | participant | Details + caller's `access` (`relation`, `can_manage`, `can_escalate`) |
| `GET` | `/api/v1/support/cases/{id}/messages` | participant | Oldest first; `after=<message_id>` or `after_sequence` (polling), `before_sequence` (history), `since` (timestamp), `limit` ≤ 100 |
| `POST` | `/api/v1/support/cases/{id}/messages` | participant | Send a message (1–5000 chars, not blank) |
| `PATCH` | `/api/v1/support/cases/{id}` | participant (see rules) | `{"status"?, "assignee_user_id"?, "note"?}`; omitted fields unchanged, `null` assignee unassigns; all-or-nothing |
| `PATCH` | `/api/v1/support/cases/{id}/status` | participant (see rules) | `{"status", "note"?}` |
| `PATCH` | `/api/v1/support/cases/{id}/assignee` | handler | `{"assignee_user_id": uuid \| null}` |
| `GET` | `/api/v1/support/cases/{id}/assignees` (alias `/assignable-users`) | handler | Users the case may be assigned to (`user_id`, `name`, `role`) |
| `POST` | `/api/v1/support/cases/{id}/escalate` | requester, owner, support staff | Escalate an attendee case to Platform Admins (`{"reason"}`) |
| `GET` | `/api/v1/support/cases/{id}/activity` | participant | Chronological lifecycle history (actor, timestamp, from/to) |
| `GET` | `/api/v1/users/me/notifications` | self | In-app notifications (`unread_only`, paging, `unread_count`) |
| `POST` | `/api/v1/users/me/notifications/{id}/read` | self | Mark one read (idempotent) |

### Routing and context

* `ATTENDEE_ORGANIZER`: a customer and the organizer staff of one event. Needs an
  event/order/ticket the customer owns (an event-only case needs an order for
  the event or a publicly visible event).
* `PLATFORM`: an organizer (about their own event, or without context) or any
  user with an account/technical issue, and Platform Admins.
* Case responses carry `requester`/`assignee` (`user_id`, `display_name`,
  `name`), flat `event_id`/`order_id`/`ticket_id`/`ticket_public_id`, and nested
  `event` (`id`, `title`), `order` (`id`, `order_number`) and `ticket` (`id`,
  `public_id`, `ticket_type_name`). Messages carry `sender` (`user_id`,
  `display_name`, `name`, `role`).
* `ticket_id` accepts the ticket id or its `public_id` and resolves its order
  and event; `order_id` resolves its event. Extra
  identifiers must agree (`422 SUPPORT_CONTEXT_MISMATCH`). Unknown and
  not-yours references both return `404 SUPPORT_CONTEXT_NOT_FOUND`.
* Categories: `TICKET_DELIVERY`, `PAYMENT`, `REFUND`, `SEATING`,
  `EVENT_INFORMATION`, `CHECK_IN`, `ACCOUNT`, `TECHNICAL`.

### Authorization matrix

| Caller | Attendee case (own event context) | Platform case |
|---|---|---|
| Requester | full participant (resolve / reopen, message, escalate) | full participant |
| Event owner | handler | — (only if requester) |
| `EVENT_ADMIN` / `ORGANIZER_STAFF` of that event | handler | no access |
| `CHECK_IN_STAFF` | no access | no access |
| Platform Admin | handler **only after escalation** | handler |
| Anyone else | `403 SUPPORT_CASE_ACCESS_DENIED`, no metadata | same |

Access is recomputed from PostgreSQL on every request (removing an EventStaff
row revokes it immediately); the assignee field never grants access by
itself. Listings put the same rule in the SQL `WHERE`, so inaccessible cases
are neither returned nor counted.

### Lifecycle

`OPEN → IN_PROGRESS | WAITING_FOR_CUSTOMER | RESOLVED`,
`IN_PROGRESS → WAITING_FOR_CUSTOMER | RESOLVED`,
`WAITING_FOR_CUSTOMER → IN_PROGRESS | RESOLVED`,
`RESOLVED → OPEN | IN_PROGRESS` (reopen). Handlers may make any listed
transition; the requester may resolve or reopen (`RESOLVED → OPEN`). A
customer reply on `WAITING_FOR_CUSTOMER` moves the case to `IN_PROGRESS`.
Messages cannot be posted on a `RESOLVED` case (reopen first).

Assignees: event owner or support staff of the event (plus Platform Admins once
escalated) for attendee cases; Platform Admins for platform cases. Active
accounts only.

### Ordering and polling

Each message gets a per-case, gap-free `sequence` under the case row lock, so
ordering is deterministic even with identical timestamps. Poll with
`after_sequence=<next_cursor>`; with no new messages `next_cursor` stays the
same.

### Notifications

In-app (`channel = IN_APP`) for: case created (handlers), new message (the
other side: assignee if any, otherwise all handlers; or the requester),
assigned/reassigned (new and previous assignee), significant status changes
(`WAITING_FOR_CUSTOMER`, `RESOLVED`, reopen), escalation (Platform Admins plus
the other side). Recipients are intersected with the users currently
authorized for the case; the actor and blocked accounts are skipped and
duplicates collapse.

## Administration, analytics and event history (SCRUM-17)

### Platform Admin endpoints (all require `PLATFORM_ADMIN`)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/admin/users` | Search `q` (email, name, id), `status`, `role`, `is_organizer`, `sort`, paging |
| `GET` | `/api/v1/admin/users/{id}` | Inspect one user (no password hash or tokens) |
| `POST` | `/api/v1/admin/users/{id}/suspend` · `/unsuspend` | `{"reason"}`; suspend = status `BLOCKED` + refresh sessions revoked |
| `GET` | `/api/v1/admin/events` · `/{id}` | Search `q`, `status`, `suspended`, `owner_user_id`, `starts_from/to`, `sort` |
| `POST` | `/api/v1/admin/events/{id}/suspend` · `/unsuspend` | Suspend a suspicious event |
| `POST` | `/api/v1/admin/events/{id}/paid-sales/stop` · `/resume` | Stop paid checkout only (free registration continues) |
| `GET` | `/api/v1/admin/orders` | Search `q` (order number, buyer email, id), `status`, `event_id`, `buyer_user_id`, dates, `sort` |
| `GET` | `/api/v1/admin/payments` | Search `q` (provider ref, order number, id), `status`, `provider`, `order_id`, `event_id`, dates |
| `GET` | `/api/v1/admin/activations` | Paid-sales activation records (`NOT_ACTIVATED` / `ACTIVE` / `SUSPENDED`) |
| `GET` | `/api/v1/admin/refunds` | Refunds by `status`, `kind`, `event_id`, dates |
| `GET` | `/api/v1/admin/failures` | Failed payments, failed refunds and failed outbox emails (`kind`) |
| `GET` · `POST` | `/api/v1/admin/disputes` | List / record a dispute against a captured payment |
| `PATCH` | `/api/v1/admin/disputes/{id}` | `OPEN → UNDER_REVIEW → RESOLVED \| REJECTED` |
| `GET` | `/api/v1/admin/campaigns` | All campaigns (`q`: name, code, event title) with `effective_status`, `redemption_count` |
| `GET` | `/api/v1/admin/export/{users\|events\|orders\|payments}.csv` | CSV export (≤ 10,000 rows; same `q` and `status` filters as the lists, invalid `status` → 422; `X-Truncated: true` when the limit cut the file, `X-Total-Count` = data rows in the file; formula-safe cells, audited) |
| `POST` | `/api/v1/admin/organizers/{user_id}/verification` | `{"status": "VERIFIED\|REJECTED\|PENDING", "reason"?}` |
| `POST` | `/api/v1/admin/payout-accounts/{id}/status` | `{"status": "ACTIVE\|BLOCKED\|PENDING", "reason"?}` |
| `GET` | `/api/v1/admin/support/escalated` | Escalated attendee support cases |
| `GET` | `/api/v1/admin/actions` | Administrative actions from the audit log |

Every administrative action writes an `admin.*` audit entry
(`actor_type = PLATFORM_ADMIN`) with the target, the admin, the reason
(`metadata.reason`), the timestamp and `before`/`after` state.

### Suspension enforcement

* **User**: `users.status = BLOCKED`. `get_current_user` re-reads the user on
  every request, so access tokens issued earlier are rejected with
  `403 USER_BLOCKED`; refresh sessions are revoked; login is refused.
* **Event** (`events.suspended_at`, orthogonal to `status`): `POST /orders`, the
  legacy `/purchase`, holds, promo quotes, starting a payment and completing a
  pending payment all refuse (`409 EVENT_SUSPENDED`). A pending payment that
  completes after suspension becomes `FAILED` (`failure_code EVENT_SUSPENDED`);
  its order is cancelled and holds and promo capacity are released. Publish and
  duplicate are blocked, and the event is hidden from the catalog, public detail
  and campaign QR resolution. Existing paid orders, valid tickets, payments and
  refunds are untouched and keep their lifecycle (refunds still work).
* **Paid sales stop** (`event_sales_activations.sales_suspended_at`): paid
  checkout and payment completion fail with `PAID_SALES_SUSPENDED`; re-running
  the activation checklist cannot lift it; free registration continues.

### Disputes

No payment provider exists, so `payment_disputes` is a minimal bookkeeping
entity recorded by Platform Admins against captured payments (one open dispute
per payment). It never changes payments, orders or refunds; money only moves
through the refund flow.

### Organizer analytics

* `GET /api/v1/events/{event_id}/analytics`: event owner, Platform Admin, or
  event staff with `permissions.view_analytics = true`. Default Event Admin
  permissions do not include analytics.
* `GET /api/v1/organizers/me/analytics`: aggregated over events the caller owns
  or has `view_analytics` on (relationship based; a global role does not widen
  it). Optional `event_id`, `ticket_type_id`, `start`, `end`.

Filters: `start` inclusive, `end` exclusive, UTC when no offset; `end <= start`
gives `422 INVALID_DATE_RANGE`. Every reference is authorized before
aggregation: a foreign `event_id` gives 403, a foreign/unknown `ticket_type_id`
gives 404. All metrics are SQL aggregates over authoritative rows (no locks, a
fixed number of queries). The response includes `definitions`.

| Metric | Formula and time basis |
|---|---|
| `capacity.total_capacity` | Σ min(event capacity, Σ ticket type capacity); snapshot |
| `capacity.sold_tickets` | order items of `PAID`/`CONFIRMED` orders; snapshot |
| `capacity.reserved_tickets` | active, unexpired holds; snapshot |
| `capacity.remaining_tickets` | capacity − sold − reserved (≥ 0); snapshot |
| `attendance.checked_in` | tickets in `CHECKED_IN` (reversal returns to `VALID`; repeated scans are rejected); snapshot |
| `attendance.eligible_tickets` | `VALID` + `CHECKED_IN` (cancelled/refunded excluded) |
| `attendance.attendance_rate` | checked_in / eligible_tickets |
| `attendance.period_*` | check-in/reversal records by `created_at`; admitted = distinct `CHECKED_IN` tickets by `checked_in_at` |
| `sales[].tickets_sold`, `orders` | items/orders with `paid_at` in range (payment confirmation; pending/failed never have `paid_at`) |
| `sales[].gross_ticket_sales` | Σ `unit_price` |
| `sales[].discounts` | Σ item `discount_amount` (already inside revenue; never subtracted twice) |
| `sales[].ticket_revenue` | Σ `final_price` |
| `sales[].fees` | Σ `fee_amount` (null with a ticket type filter) |
| `sales[].gross_revenue` | Σ `total_amount` charged = ticket_revenue + fees |
| `sales[].refunds` | Σ `SUCCEEDED` refund amounts by `completed_at` |
| `sales[].net_revenue` | gross_revenue − refunds |
| `sales[].estimated_payout` | Σ `final_price` of orders paid in range that are not `CANCELLED`/`REFUNDED`; no fees; null with a ticket type filter |
| `campaigns[]` | redemption counts (snapshot) and attributed orders/tickets/discounts/revenue by `paid_at` |

Money is grouped per currency and never combined.

`sales_over_time` (added after PR 19, additive) lists one entry per day and
currency for the same period as `sales`: `{date, currency, orders, tickets,
gross}`, where `gross` is list prices (no fees, like `gross_ticket_sales`). Days
are bucketed by `orders.paid_at` in the event timezone (`scope.timezone`; UTC
for an aggregate over several events), so a chart needs one request instead of
one per bar.

**Processing fee**: `gross_ticket_sales`, `discounts`, `ticket_revenue` and
`sales_over_time.gross` exclude the 3% fee; `fees` is the fee; `gross_revenue`,
`refunds` and `net_revenue` include it (refunds return the full amount charged).

**Estimated payout (demo only, no money moves)**: the ticket price after
discounts (`final_price`) of orders paid in `[start, end)` that are not
cancelled or refunded now. Processing fees are paid by the buyer to the
platform and are excluded; the one-time paid-sales activation fee is not
deducted. A refund removes the order from the period in which it was paid. The
value is a decimal string like other amounts and is `null` with `ticket_type_id`,
like `fees`.

### Event history and timeline

* `GET /api/v1/organizers/me/events`: events the caller owns or is
  `EVENT_ADMIN`/`ORGANIZER_STAFF` of. `category` (repeatable): `CANCELLED`
  (status), `COMPLETED` (status `COMPLETED` or ended), `ACTIVE` (started, not
  ended), `UPCOMING` (not started). Suspension is reported as `is_suspended` and
  never changes the category. Also `status`, `q`, `suspended`,
  `starts_from/to`, `sort`, paging.
* `GET /api/v1/events/{event_id}/audit-log`: also accepts `from`/`to`
  (inclusive dates, event timezone) and `action_prefix`; entries add
  `actor_name` and `entity_label` (e.g. `Order #1042`, a ticket type name).
* `GET /api/v1/events/{event_id}/timeline` (owner or Platform Admin): the
  event's entries in the append-only audit log, newest first (`order=asc`
  available). `action` filters are repeatable (exact, or a prefix ending in `.`
  or `.*`); `entity_type`, `start`/`end` and paging are also available. Each
  entry has actor (id, type, name), timestamp, action, entity type/id/label and a
  redacted description (no raw payloads). Access-denied probes are excluded,
  and nothing is synthesized for actions that were never recorded.

### Migration

`sql/migrate_009_support_admin_analytics.sql` (in the default `SCHEMA_FILES`):
support routing/escalation/resolution columns, the `WAITING_FOR_CUSTOMER` enum
value, the message `sequence`/`sender_role` columns (backfilled by
`created_at, id`), legacy category normalization (originals kept in
`audit_logs`), user/event/paid-sales suspension columns, the
`payment_disputes` table, and read-path indexes. Additive; nothing is dropped.
Apply with `make migrate` (or on startup).

## Frontend request (SCRUM-21/23/24/25)

* **Organizer cancel/refund**: `POST /orders/{id}/cancel` and `/refund` accept
  the event owner (and Platform Admins) as well as the buyer. Owner refunds are
  full refunds (`kind = ORGANIZER_REQUEST`) that bypass the customer policy,
  deadline and start-time check; checked-in orders are still refused. Free
  registrations are cancelled without a refund. Other organizers get 403.
* **Demo self-service** (`DEMO_SIMULATIONS_ENABLED`, default `true`; DEMO ONLY,
  audited): `POST /organizers/me/verification/simulate` (profile → VERIFIED),
  `POST /organizers/me/payout-account/simulate-approval` (account → ACTIVE),
  `GET /organizers/me/payout-account` (default or newest, reference masked).
  With the flag off, Platform Admins use the admin routes above.
* **Event page**: `GET /events/{id}/ticket-types` items have `available`
  (capacity − sold − active holds, capped by remaining event capacity; public).
  `GET /events/{id}` has `paid_sales_active` (activated, not stopped, event not
  suspended).
* **Attendees**: `POST /orders` accepts optional
  `attendees: [{ticket_type_id, full_name, email}]`, exactly one per ticket
  (422 otherwise). Omitted: every ticket is issued to the buyer, as before.
* **Campaign analytics** responses include `tickets_sold` (tickets of
  attributed orders that are currently PAID/CONFIRMED).
* **COMPLETED**: the background loop moves PUBLISHED events whose `ends_at` has
  passed to `COMPLETED` (audited as `SYSTEM`, `event.completed`).
* **Support** (all additive to PR 19): `ticket_public_id` and nested `event`,
  `order`, `ticket` objects on cases; `name` next to `display_name` on people;
  `scope=mine|event|all`; `?after=<message_id>`; combined
  `PATCH /support/cases/{id}`; `/assignees`. Categories unchanged
  (`EVENT_INFORMATION`, ...).
* **First Platform Admin**: register and verify an account through the API,
  then on the server run `make admin EMAIL=you@example.com`
  (`python -m app.db.bootstrap_admin --email ...`; in Docker:
  `docker compose exec api python -m app.db.bootstrap_admin --email ...`).
  It promotes the existing account (idempotent, audited as SYSTEM); there is
  no HTTP route for this by design.
* **Migration**: `sql/migrate_010_organizer_refunds.sql` widens
  `refunds_kind_valid` with `ORGANIZER_REQUEST`. `migrate_009` is unchanged
  from PR 19 (already applied in deployments).

## Reported events, platform settings, image upload, calendar, health (SRS 4.12, 4.2, 8)

### Reported events

* `POST /events/{event_id}/reports` (verified user): `{reason, details?}` with
  `reason` one of `FRAUD`, `MISLEADING`, `INAPPROPRIATE`, `PROHIBITED`, `SPAM`,
  `OTHER` (`OTHER` requires `details`). Only publicly visible events (404
  otherwise); owners cannot report their own event (409
  `CANNOT_REPORT_OWN_EVENT`); one OPEN/UNDER_REVIEW report per user and event
  (409 `REPORT_ALREADY_OPEN`). The owner is never told who reported.
* `GET /users/me/reports`: the caller's reports.
* `GET /admin/reports` (`status`, `reason`, `event_id`, paging),
  `GET /admin/reports/{id}`: include the event, reporter, whether the event is
  suspended and `event_active_reports` (open reports on the same event).
* `PATCH /admin/reports/{id}` `{status, resolution_note?, suspend_event?}`:
  OPEN ↔ UNDER_REVIEW → RESOLVED | DISMISSED (terminal). `suspend_event: true`
  is allowed only with `RESOLVED` and uses `resolution_note` as the suspension
  reason (same effect and audit as `POST /admin/events/{id}/suspend`). The
  reporter gets an `EVENT_REPORT_REVIEWED` in-app notification.

### Platform settings

* `GET /admin/settings`, `PATCH /admin/settings`
  `{activation_fee_amount?, processing_fee_percent?, reason?}` (fee ≥ 0,
  percent 0–20). Audited as `admin.platform_settings_updated` (visible in
  `GET /admin/actions`). Defaults are the old hard-coded values: 1000.00 KZT
  and 3%.
* New orders and promo quotes use the current percent; existing orders keep
  their stored `fee_amount`. An unpaid activation fee follows the current
  setting; a paid one is never changed.

### Event image upload

* `POST /events/{event_id}/images/upload` (owner or Platform Admin), multipart
  field `file`: JPEG, PNG or WebP, at most `MEDIA_MAX_UPLOAD_BYTES` (5 MB).
  The image is decoded and re-encoded (metadata such as GPS is removed); other
  files get 415, larger ones 413. The response `object_key` is the image URL:
  put it in `images` on `PATCH /events/{id}` to attach it (same contract as
  before, external URLs still work).
* `GET /media/{key}` serves stored files (public, immutable cache headers).
  Files live under `MEDIA_ROOT` (`media/` locally, the `media_data` volume in
  Docker). The URL is `{PUBLIC_BASE_URL}/api/v1/media/...`; with
  `PUBLIC_BASE_URL` empty it is relative, which works through the web app's
  `/api/v1` proxy but not for the mobile app.

### Calendar export

* `GET /events/{event_id}/calendar.ics`: RFC 5545 file, same visibility as
  `GET /events/{id}`. `URL` points to `{PUBLIC_WEB_BASE_URL}/catalog/{id}` when
  that is set; cancelled events have `STATUS:CANCELLED`.

### Health checks

* `GET /health/live`: the process is up.
* `GET /health/ready`: `SELECT 1` on PostgreSQL and `PING` on Redis (when
  configured); 503 with `checks` when one fails. Used by the Docker healthcheck.

### Migration

`sql/migrate_011_reports_settings.sql` adds `event_reports` and
`platform_settings` (seeded with id 1). Additive; apply with `make migrate` or
on startup. New dependencies: `python-multipart`, `pillow` (already installed
through `qrcode[pil]`).

## Endpoint reference

All paths are under `/api/v1`. Interactive docs with request/response schemas: `/docs` (Swagger UI) and `/openapi.json`.

### Auth

| Method | Path |
|---|---|
| `POST` | `/auth/register` |
| `POST` | `/auth/verify-email` |
| `POST` | `/auth/resend-verification` |
| `POST` | `/auth/login` |
| `POST` | `/auth/refresh` |
| `POST` | `/auth/logout` |
| `POST` | `/auth/logout-all` |
| `POST` | `/auth/request-password-reset` |
| `POST` | `/auth/reset-password` |

### Users

| Method | Path |
|---|---|
| `GET` | `/users/me` |
| `PATCH` | `/users/me` |
| `GET` | `/users/me/notifications` |
| `POST` | `/users/me/notifications/{notification_id}/read` |
| `GET` | `/users/me/reports` |

### Organizers

| Method | Path |
|---|---|
| `POST` | `/organizers/me` |
| `GET` | `/organizers/me` |
| `PATCH` | `/organizers/me` |
| `POST` | `/organizers/me/verification/simulate` |
| `GET` | `/organizers/me/payout-account` |
| `POST` | `/organizers/me/payout-account/simulate-approval` |
| `PUT` | `/organizers/me/payout-account` |
| `GET` | `/organizers/me/events` |
| `GET` | `/organizers/me/analytics` |

### Events, ticket types, inventory, paid sales

| Method | Path |
|---|---|
| `GET` | `/events/assigned` |
| `GET` | `/events/{event_id}/admins` |
| `POST` | `/events/{event_id}/admins` |
| `DELETE` | `/events/{event_id}/admins/{user_id}` |
| `GET` | `/events/catalog` |
| `POST` | `/events` |
| `GET` | `/events` |
| `GET` | `/events/{event_id}` |
| `PATCH` | `/events/{event_id}` |
| `DELETE` | `/events/{event_id}` |
| `POST` | `/events/{event_id}/duplicate` |
| `GET` | `/events/{event_id}/preview` |
| `POST` | `/events/{event_id}/publish` |
| `POST` | `/events/{event_id}/unpublish` |
| `POST` | `/events/{event_id}/cancel` |
| `GET` | `/events/{event_id}/cancellation` |
| `GET` | `/events/{event_id}/refund-policy` |
| `PUT` | `/events/{event_id}/refund-policy` |
| `GET` | `/events/{event_id}/audit-log` |
| `GET` | `/events/{event_id}/paid-sales/activation` |
| `POST` | `/events/{event_id}/paid-sales/terms` |
| `POST` | `/events/{event_id}/paid-sales/activation-fee` |
| `POST` | `/events/{event_id}/paid-sales/activate` |
| `POST` | `/events/{event_id}/ticket-types` |
| `GET` | `/events/{event_id}/ticket-types` |
| `PATCH` | `/events/{event_id}/ticket-types/{ticket_type_id}` |
| `DELETE` | `/events/{event_id}/ticket-types/{ticket_type_id}` |
| `GET` | `/events/{event_id}/attendees` |
| `GET` | `/events/{event_id}/inventory` |
| `POST` | `/events/{event_id}/ticket-types/{ticket_type_id}/holds` |
| `POST` | `/events/{event_id}/ticket-types/{ticket_type_id}/purchase` |
| `GET` | `/events/{event_id}/analytics` |
| `GET` | `/events/{event_id}/timeline` |
| `POST` | `/events/{event_id}/images/upload` |
| `GET` | `/events/{event_id}/calendar.ics` |
| `POST` | `/events/{event_id}/reports` |

### Campaigns

| Method | Path |
|---|---|
| `POST` | `/events/{event_id}/campaigns` |
| `GET` | `/events/{event_id}/campaigns` |
| `GET` | `/events/{event_id}/campaigns/analytics` |
| `GET` | `/events/{event_id}/campaigns/{campaign_id}` |
| `PATCH` | `/events/{event_id}/campaigns/{campaign_id}` |
| `GET` | `/events/{event_id}/campaigns/{campaign_id}/analytics` |
| `GET` | `/events/{event_id}/campaigns/{campaign_id}/qr.png` |
| `POST` | `/campaigns/validate` |
| `GET` | `/campaigns/qr/{campaign_token}` |

### Orders, payments, refunds

| Method | Path |
|---|---|
| `POST` | `/orders` |
| `POST` | `/orders/{order_id}/payments/simulate` |
| `POST` | `/orders/payments/{payment_id}/simulate-result` |
| `GET` | `/orders` |
| `GET` | `/orders/{order_id}/tickets` |
| `GET` | `/orders/{order_id}` |
| `POST` | `/orders/{order_id}/confirm` |
| `POST` | `/orders/{order_id}/checkout` |
| `POST` | `/orders/{order_id}/cancel` |
| `POST` | `/orders/{order_id}/refund` |
| `GET` | `/orders/{order_id}/refunds` |
| `GET` | `/orders/{order_id}/refund-eligibility` |

### Tickets and check-in

| Method | Path |
|---|---|
| `GET` | `/tickets` |
| `GET` | `/tickets/{public_id}` |
| `GET` | `/tickets/{public_id}/pdf` |
| `GET` | `/tickets/{public_id}/download` |
| `POST` | `/events/{event_id}/tickets/validate` |
| `POST` | `/events/{event_id}/validate` |
| `POST` | `/events/{event_id}/check-in` |
| `POST` | `/events/{event_id}/checkin` |
| `POST` | `/events/{event_id}/tickets/check-in` |
| `GET` | `/events/{event_id}/tickets/search` |
| `GET` | `/events/{event_id}/attendees/search` |
| `GET` | `/events/{event_id}/tickets/stats` |
| `POST` | `/events/{event_id}/tickets/{public_id}/check-in` |
| `POST` | `/events/{event_id}/tickets/{public_id}/reverse-check-in` |

### Support

| Method | Path |
|---|---|
| `POST` | `/support/cases` |
| `GET` | `/support/cases` |
| `GET` | `/support/cases/{case_id}` |
| `GET` | `/support/cases/{case_id}/messages` |
| `POST` | `/support/cases/{case_id}/messages` |
| `PATCH` | `/support/cases/{case_id}` |
| `PATCH` | `/support/cases/{case_id}/status` |
| `PATCH` | `/support/cases/{case_id}/assignee` |
| `GET` | `/support/cases/{case_id}/assignees` |
| `GET` | `/support/cases/{case_id}/assignable-users` |
| `POST` | `/support/cases/{case_id}/escalate` |
| `GET` | `/support/cases/{case_id}/activity` |

### Admin (`PLATFORM_ADMIN` only)

| Method | Path |
|---|---|
| `GET` | `/admin/users` |
| `GET` | `/admin/users/{user_id}` |
| `POST` | `/admin/users/{user_id}/suspend` |
| `POST` | `/admin/users/{user_id}/unsuspend` |
| `GET` | `/admin/events` |
| `GET` | `/admin/events/{event_id}` |
| `POST` | `/admin/events/{event_id}/suspend` |
| `POST` | `/admin/events/{event_id}/unsuspend` |
| `POST` | `/admin/events/{event_id}/paid-sales/stop` |
| `POST` | `/admin/events/{event_id}/paid-sales/resume` |
| `GET` | `/admin/orders` |
| `GET` | `/admin/payments` |
| `GET` | `/admin/activations` |
| `GET` | `/admin/refunds` |
| `GET` | `/admin/failures` |
| `GET` | `/admin/support/escalated` |
| `GET` | `/admin/actions` |
| `GET` | `/admin/campaigns` |
| `GET` | `/admin/export/{kind}.csv` |
| `GET` | `/admin/disputes` |
| `POST` | `/admin/disputes` |
| `PATCH` | `/admin/disputes/{dispute_id}` |
| `POST` | `/admin/organizers/{user_id}/verification` |
| `GET` | `/admin/payout-accounts` |
| `POST` | `/admin/payout-accounts/{account_id}/status` |
| `GET` | `/admin/reports` |
| `GET` | `/admin/reports/{report_id}` |
| `PATCH` | `/admin/reports/{report_id}` |
| `GET` | `/admin/settings` |
| `PATCH` | `/admin/settings` |

### Media

| Method | Path |
|---|---|
| `GET` | `/media/{key:path}` |

### Health (no prefix)

| Method | Path |
|---|---|
| `GET` | `/health/live` |
| `GET` | `/health/ready` |
