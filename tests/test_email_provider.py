import json
from datetime import datetime, timezone

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.services.email import (
    BrevoEmailProvider,
    EmailTicket,
    OrderConfirmationEmail,
    PermanentEmailError,
    TransientEmailError,
    build_email_provider,
)


def _provider(handler, **overrides):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    options = {
        "api_key": "test-api-key",
        "sender_email": "no-reply@example.com",
        "sender_name": "BiletFlow",
        "client": client,
    }
    options.update(overrides)
    return BrevoEmailProvider(**options), client


def _settings(**overrides) -> Settings:
    values = {
        "app_env": "test",
        "jwt_access_secret": "a" * 64,
        "jwt_refresh_secret": "b" * 64,
    }
    values.update(overrides)
    return Settings(**values)


async def test_brevo_verification_payload_and_sender():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(201, json={"messageId": "m-1"})

    provider, client = _provider(handler)
    try:
        await provider.send_verification_code(
            email="User@Example.com", code="123456", locale="en"
        )
    finally:
        await client.aclose()

    assert len(captured) == 1
    request = captured[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.brevo.com/v3/smtp/email"
    assert request.headers["api-key"] == "test-api-key"
    body = json.loads(request.content)
    assert body["sender"] == {"email": "no-reply@example.com", "name": "BiletFlow"}
    assert body["to"] == [{"email": "User@example.com"}]
    assert "123456" in body["textContent"]
    assert "123456" in body["htmlContent"]


async def test_order_confirmation_html_is_escaped():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(201, json={"messageId": "m-2"})

    provider, client = _provider(handler)
    message = OrderConfirmationEmail(
        email="buyer@example.com",
        order_number=42,
        event_name="<script>alert(1)</script>",
        event_starts_at=datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
        venue="Hall & Stage",
        tickets=[
            EmailTicket(
                public_id="00000000-0000-0000-0000-000000000001",
                ticket_type_name="<b>VIP</b>",
                attendee_name="A & B",
                download_url="https://example.com/tickets/1/pdf?x=1&y=2",
            )
        ],
    )
    try:
        await provider.send_order_confirmation(message)
    finally:
        await client.aclose()

    body = json.loads(captured[0].content)
    html = body["htmlContent"]
    assert "&lt;script&gt;" in html
    assert "<script>" not in html
    assert "Hall &amp; Stage" in html
    assert "&lt;b&gt;VIP&lt;/b&gt;" in html
    assert "A &amp; B" in html
    assert "x=1&amp;y=2" in html


@pytest.mark.parametrize(
    ("status", "expected"),
    [(400, PermanentEmailError), (401, PermanentEmailError), (403, PermanentEmailError)],
)
async def test_brevo_permanent_statuses(status, expected):
    provider, client = _provider(
        lambda request: httpx.Response(status, json={"code": "bad_request"})
    )
    try:
        with pytest.raises(expected) as exc_info:
            await provider.send_verification_code(email="a@example.com", code="123456")
    finally:
        await client.aclose()

    assert exc_info.value.retryable is False
    assert exc_info.value.status_code == status
    assert exc_info.value.provider_code == "bad_request"


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_brevo_server_errors_are_transient(status):
    provider, client = _provider(
        lambda request: httpx.Response(status, json={"code": "server_error"})
    )
    try:
        with pytest.raises(TransientEmailError) as exc_info:
            await provider.send_verification_code(email="a@example.com", code="123456")
    finally:
        await client.aclose()

    assert exc_info.value.retryable is True
    assert exc_info.value.status_code == status


async def test_brevo_rate_limit_is_transient_and_honours_retry_after():
    provider, client = _provider(
        lambda request: httpx.Response(
            429,
            headers={"retry-after": "7"},
            json={"code": "too_many_requests"},
        )
    )
    try:
        with pytest.raises(TransientEmailError) as exc_info:
            await provider.send_verification_code(email="a@example.com", code="123456")
    finally:
        await client.aclose()

    assert exc_info.value.retry_after_seconds == 7
    assert exc_info.value.provider_code == "too_many_requests"


async def test_brevo_timeout_is_transient_and_ambiguous():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    provider, client = _provider(handler)
    try:
        with pytest.raises(TransientEmailError) as exc_info:
            await provider.send_verification_code(email="a@example.com", code="123456")
    finally:
        await client.aclose()

    assert exc_info.value.retryable is True
    assert exc_info.value.ambiguous is True


def test_brevo_requires_valid_configuration():
    with pytest.raises(ValueError):
        BrevoEmailProvider(api_key="", sender_email="a@example.com", sender_name="X")
    with pytest.raises(ValueError):
        BrevoEmailProvider(
            api_key="key", sender_email="no-reply@biletflow.local", sender_name="X"
        )


def test_provider_selection_uses_dev_in_tests():
    settings = _settings(email_provider="brevo", brevo_api_key="key")
    # APP_ENV=test must never make outbound mail calls even if a developer
    # accidentally leaves production settings in the environment.
    assert build_email_provider(settings).__class__.__name__ == "DevEmailProvider"


def test_production_requires_brevo():
    with pytest.raises(ValidationError):
        _settings(app_env="production", email_provider="dev")


def test_sender_falls_back_to_email_from():
    settings = _settings(
        email_from="BiletFlow Tickets <tickets@example.com>",
        brevo_sender_email="",
        brevo_sender_name="",
    )
    assert settings.effective_email_sender == ("BiletFlow Tickets", "tickets@example.com")
