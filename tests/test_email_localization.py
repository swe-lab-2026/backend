"""Transactional email localization (en / ru / kk) and retryable delivery."""

from datetime import datetime, timezone
from uuid import UUID

import pytest

from app.db import repositories as repo
from app.services import notifications
from app.services.email import (
    EmailTicket,
    NotificationEmail,
    OrderConfirmationEmail,
    TransientEmailError,
)
from app.services.email_templates import (
    _COPY,
    normalize_locale,
    render_notification_email,
    render_order_confirmation_email,
    render_password_reset_email,
    render_verification_email,
)
from tests.conftest import API, bearer, get_user
from tests.test_tickets import BUYER_EMAIL, _setup_event

STARTS = datetime(2030, 1, 1, 18, 0, tzinfo=timezone.utc)
KINDS = ("PAYMENT_FAILED", "EVENT_UPDATED", "EVENT_CANCELLED", "REFUND_COMPLETED")


def _order_message(locale: str) -> OrderConfirmationEmail:
    return OrderConfirmationEmail(
        email="buyer@example.com", order_number=7, event_name="<b>Show</b>",
        event_starts_at=STARTS, venue=None, locale=locale,
        tickets=[EmailTicket(public_id="p-1", ticket_type_name="VIP", attendee_name=None,
                             download_url="https://api.example.com/tickets/p-1/pdf")],
    )


def _notification(kind: str, locale: str) -> NotificationEmail:
    return NotificationEmail(
        email="buyer@example.com", kind=kind, order_number=7, event_name="Show",
        event_starts_at=STARTS, venue="Hall", locale=locale,
        details={"refund_amount": "10.00 KZT", "custom_key": "kept"},
    )


def test_every_locale_defines_every_string():
    for locale in ("ru", "kk"):
        assert set(_COPY[locale]) == set(_COPY["en"]), locale


@pytest.mark.parametrize(("raw", "expected"), [
    ("ru", "ru"), ("ru-RU", "ru"), ("KK", "kk"), ("kk_KZ", "kk"),
    ("de", "en"), ("", "en"), (None, "en"),
])
def test_normalize_locale(raw, expected):
    assert normalize_locale(raw) == expected


@pytest.mark.parametrize("locale", ["ru", "kk"])
def test_account_emails_are_localized(locale):
    verify = render_verification_email(code="123456", locale=locale)
    reset = render_password_reset_email(token="tok-abc", locale=locale)
    assert verify.subject == _COPY[locale]["verify.subject"]
    assert reset.subject == _COPY[locale]["reset.subject"]
    assert "123456" in verify.text and "123456" in verify.html
    assert "tok-abc" in reset.text and "tok-abc" in reset.html
    for content in (verify, reset):
        assert f'lang="{locale}"' in content.html
        assert "verification code is" not in content.text
        assert "password reset was requested" not in content.text


@pytest.mark.parametrize("locale", ["en", "ru", "kk"])
def test_order_confirmation_is_localized_and_escaped(locale):
    content = render_order_confirmation_email(_order_message(locale))
    copy = _COPY[locale]
    assert copy["order.lead"].format(number=7) in content.text
    assert copy["venue_tba"] in content.text
    assert copy["ticket_holder"] in content.text
    assert copy["order.download_pdf"] in content.html
    assert "<b>Show</b>" not in content.html and "&lt;b&gt;Show&lt;/b&gt;" in content.html
    assert "2030-01-01 18:00 UTC" in content.text


@pytest.mark.parametrize("locale", ["ru", "kk"])
@pytest.mark.parametrize("kind", KINDS)
def test_lifecycle_notifications_are_localized(kind, locale):
    content = render_notification_email(_notification(kind, locale))
    copy = _COPY[locale]
    assert content.subject.startswith(copy[f"notify.{kind}.prefix"])
    assert "№7" in content.subject
    assert copy[f"notify.{kind}.lead"] in content.text
    assert f"{copy['detail.refund_amount']}: 10.00 KZT" in content.text
    assert "custom_key: kept" in content.text  # unknown detail keys pass through
    assert _COPY["en"][f"notify.{kind}.lead"] not in content.text


def test_unsupported_locale_falls_back_to_english():
    content = render_notification_email(_notification("REFUND_COMPLETED", "de"))
    assert content.subject.startswith("Refund completed") and "#7" in content.subject
    assert 'lang="en"' in content.html


# ---------- API: locale preference reaches the email ----------

async def test_register_and_profile_locale_drive_account_emails(client, app):
    email = "lokal@example.com"
    registered = await client.post(f"{API}/auth/register", json={
        "email": email, "password": "StrongPassword123!", "first_name": "Aru",
        "last_name": "Serik", "locale": "kk"})
    assert registered.status_code == 201, registered.text
    provider = app.state.email_provider
    assert provider.last_locale_for(email) == "kk"

    rejected = await client.post(f"{API}/auth/register", json={
        "email": "bad-locale@example.com", "password": "StrongPassword123!",
        "first_name": "A", "last_name": "B", "locale": "de"})
    assert rejected.status_code == 422

    code = provider.last_code_for(email)
    assert (await client.post(f"{API}/auth/verify-email",
                              json={"email": email, "code": code})).status_code == 200
    login = await client.post(f"{API}/auth/login",
                              json={"email": email, "password": "StrongPassword123!"})
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    me = await client.get(f"{API}/users/me", headers=bearer(token))
    assert me.json()["locale"] == "kk"

    patched = await client.patch(f"{API}/users/me", json={"locale": "ru"},
                                 headers=bearer(token))
    assert patched.status_code == 200 and patched.json()["locale"] == "ru"
    assert (await client.patch(f"{API}/users/me", json={"locale": "fr"},
                               headers=bearer(token))).status_code == 422

    reset = await client.post(f"{API}/auth/request-password-reset", json={"email": email})
    assert reset.status_code in (200, 202), reset.text
    assert provider.last_locale_for(email) == "ru"


async def test_order_confirmation_uses_buyer_locale_and_retries_transient_failure(
    client, app, db
):
    event_id, ticket_type_id, _owner, _owner_token, buyer, buyer_token = (
        await _setup_event(client, app, db)
    )
    buyer_row = await get_user(db, BUYER_EMAIL)
    assert buyer_row is not None
    buyer_row.locale = "ru"
    await db.commit()

    response = await client.post(
        f"{API}/orders",
        json={"event_id": event_id, "items": [{"ticket_type_id": ticket_type_id, "quantity": 1}]},
        headers=bearer(buyer_token),
    )
    assert response.status_code == 201, response.text
    order_id = UUID(response.json()["id"])
    sent = app.state.email_provider.last_order_confirmation_for(BUYER_EMAIL)
    assert sent is not None and sent.locale == "ru"
    assert render_order_confirmation_email(sent).subject.startswith("Заказ №")

    # Replay delivery: the first attempt fails transiently, the retry succeeds.
    notification = await repo.get_order_notification(
        db, order_id=order_id, kind=notifications.KIND_ORDER_CONFIRMATION
    )
    assert notification is not None
    notification.status = "FAILED"
    notification.attempts = 0
    notification.sent_at = None
    await db.commit()

    delivered: list[OrderConfirmationEmail] = []

    class FlakyProvider:
        calls = 0

        async def send_order_confirmation(self, message) -> None:
            FlakyProvider.calls += 1
            if FlakyProvider.calls == 1:
                raise TransientEmailError("Brevo reported a temporary server error")
            delivered.append(message)

    provider = FlakyProvider()
    options = {"download_base": "http://test/api/v1", "max_attempts": 5,
               "retry_base_seconds": 0}
    assert await notifications.process_pending_notifications(
        db, provider=provider, **options) == 0
    await db.commit()
    await db.refresh(notification)
    assert notification.status == "FAILED" and notification.attempts == 1
    assert notification.last_error

    assert await notifications.process_pending_notifications(
        db, provider=provider, **options) == 1
    await db.commit()
    await db.refresh(notification)
    assert notification.status == "SENT" and notification.attempts == 2
    assert notification.last_error is None
    assert [m.locale for m in delivered] == ["ru"]
    del buyer
