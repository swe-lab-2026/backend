import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

import httpx
from email_validator import EmailNotValidError, validate_email

from app.core.config import Settings, get_settings
from app.services.email_templates import (
    render_order_confirmation_email,
    render_password_reset_email,
    render_verification_email,
)

logger = logging.getLogger("app.email")


@dataclass(frozen=True)
class EmailTicket:
    public_id: str
    ticket_type_name: str
    attendee_name: str | None
    download_url: str


@dataclass(frozen=True)
class OrderConfirmationEmail:
    email: str
    order_number: int
    event_name: str
    event_starts_at: datetime
    venue: str | None
    tickets: list[EmailTicket] = field(default_factory=list)
    locale: str = "en"


class EmailDeliveryError(Exception):
    """Base class for provider delivery failures.

    ``retryable`` tells the notification outbox whether a later attempt may
    succeed. ``ambiguous`` marks cases such as a read timeout where the provider
    may already have accepted the message.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        status_code: int | None = None,
        provider_code: str | None = None,
        retry_after_seconds: int | None = None,
        ambiguous: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.provider_code = provider_code
        self.retry_after_seconds = retry_after_seconds
        self.ambiguous = ambiguous


class TransientEmailError(EmailDeliveryError):
    def __init__(self, message: str, **kwargs: object) -> None:
        super().__init__(message, retryable=True, **kwargs)  # type: ignore[arg-type]


class PermanentEmailError(EmailDeliveryError):
    def __init__(self, message: str, **kwargs: object) -> None:
        super().__init__(message, retryable=False, **kwargs)  # type: ignore[arg-type]


class EmailProvider(Protocol):
    """Delivery channel for transactional emails."""

    async def send_verification_code(self, *, email: str, code: str, locale: str = "en") -> None:
        ...

    async def send_password_reset(self, *, email: str, token: str, locale: str = "en") -> None:
        ...

    async def send_order_confirmation(self, message: OrderConfirmationEmail) -> None:
        ...

    async def aclose(self) -> None:
        ...


class DevEmailProvider:
    """Safe development implementation: logs messages and never reaches a mail server.

    The last message per address is kept in memory so local developers and tests
    can assert delivery without a mail server. It is not durable, is never used
    in production, and nothing in the API responses returns verification codes.
    """

    def __init__(self) -> None:
        self._last_codes: dict[str, str] = {}
        self._last_reset_tokens: dict[str, str] = {}
        self._order_confirmations: list[OrderConfirmationEmail] = []

    async def send_verification_code(self, *, email: str, code: str, locale: str = "en") -> None:
        self._last_codes[email] = code
        logger.info("verification code for %s: %s", email, code)

    def last_code_for(self, email: str) -> str | None:
        return self._last_codes.get(email)

    async def send_password_reset(self, *, email: str, token: str, locale: str = "en") -> None:
        self._last_reset_tokens[email] = token
        # The raw token is deliberately never logged: unlike a short-lived
        # verification code it is a bearer credential, and production logs
        # must not contain it either.
        logger.info("password reset token issued for %s", email)

    def last_reset_token_for(self, email: str) -> str | None:
        return self._last_reset_tokens.get(email)

    async def send_order_confirmation(self, message: OrderConfirmationEmail) -> None:
        self._order_confirmations.append(message)
        logger.info(
            "order confirmation for %s (order #%s, %d ticket(s))",
            message.email,
            message.order_number,
            len(message.tickets),
        )

    def last_order_confirmation_for(self, email: str) -> OrderConfirmationEmail | None:
        for message in reversed(self._order_confirmations):
            if message.email.lower() == email.lower():
                return message
        return None

    def order_confirmations(self) -> list[OrderConfirmationEmail]:
        return list(self._order_confirmations)

    async def aclose(self) -> None:
        return None


def _normalized_address(value: str) -> str:
    return validate_email(value, check_deliverability=False).normalized


def _retry_after_seconds(response: httpx.Response) -> int | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(int(float(raw)), 0)
    except (TypeError, ValueError):
        return None


def _error_code(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        code = body.get("code")
        if isinstance(code, str) and code:
            return code[:100]
    return None


class BrevoEmailProvider:
    """Brevo transactional email REST API adapter.

    The client is created once by the FastAPI lifespan and reused for every
    message. Only HTTP metadata and the provider error code are logged; API
    keys, verification codes, reset tokens and message contents never reach
    the logs.
    """

    def __init__(
        self,
        *,
        api_key: str,
        sender_email: str,
        sender_name: str,
        api_base_url: str = "https://api.brevo.com",
        timeout_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("BREVO_API_KEY is required")
        try:
            normalized_sender = _normalized_address(sender_email)
        except EmailNotValidError as exc:
            raise ValueError("A valid Brevo sender email is required") from exc

        self._api_key = api_key.strip()
        self._sender_email = normalized_sender
        self._sender_name = sender_name.strip() or "BiletFlow"
        base = api_base_url.rstrip("/")
        if base.endswith("/v3"):
            self._endpoint = f"{base}/smtp/email"
        else:
            self._endpoint = f"{base}/v3/smtp/email"
        self._timeout = httpx.Timeout(
            timeout_seconds, connect=min(timeout_seconds, 5.0)
        )
        self._client = client or httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=False,
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        return {
            "api-key": self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _payload(self, *, to_email: str, subject: str, text: str, html: str) -> dict[str, object]:
        try:
            recipient = _normalized_address(to_email)
        except EmailNotValidError as exc:
            raise PermanentEmailError("Invalid recipient email address") from exc

        if not subject.strip() or not text.strip() or not html.strip():
            raise PermanentEmailError("Email subject and body must not be empty")

        return {
            "sender": {"email": self._sender_email, "name": self._sender_name},
            "to": [{"email": recipient}],
            "subject": subject,
            "textContent": text,
            "htmlContent": html,
        }

    async def _post(self, payload: dict[str, object], *, kind: str) -> None:
        try:
            response = await self._client.post(
                self._endpoint,
                json=payload,
                headers=self._headers(),
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            # The request may have reached Brevo before the response was lost.
            raise TransientEmailError(
                "Brevo request timed out before a response was received",
                ambiguous=True,
            ) from exc
        except httpx.TransportError as exc:
            raise TransientEmailError(
                f"Brevo transport error ({type(exc).__name__})"
            ) from exc

        if response.status_code == 201:
            logger.info("Brevo accepted %s email", kind)
            return

        status = response.status_code
        provider_code = _error_code(response)
        retry_after = _retry_after_seconds(response)
        logger.warning(
            "Brevo rejected %s email (status=%s, code=%s)",
            kind,
            status,
            provider_code or "unknown",
        )

        if status == 429:
            raise TransientEmailError(
                "Brevo rate limited the request",
                status_code=status,
                provider_code=provider_code,
                retry_after_seconds=retry_after,
            )
        if 500 <= status <= 599:
            raise TransientEmailError(
                "Brevo reported a temporary server error",
                status_code=status,
                provider_code=provider_code,
                retry_after_seconds=retry_after,
            )
        if status in (401, 403):
            raise PermanentEmailError(
                "Brevo authentication or authorization failed",
                status_code=status,
                provider_code=provider_code,
            )
        if status == 400:
            raise PermanentEmailError(
                "Brevo rejected the email payload",
                status_code=status,
                provider_code=provider_code,
            )
        raise PermanentEmailError(
            f"Brevo rejected the email request (HTTP {status})",
            status_code=status,
            provider_code=provider_code,
        )

    async def send_verification_code(self, *, email: str, code: str, locale: str = "en") -> None:
        if not code.strip():
            raise PermanentEmailError("Verification code must not be empty")
        content = render_verification_email(code=code, locale=locale)
        payload = self._payload(
            to_email=email,
            subject=content.subject,
            text=content.text,
            html=content.html,
        )
        await self._post(payload, kind="verification")

    async def send_password_reset(self, *, email: str, token: str, locale: str = "en") -> None:
        if not token.strip():
            raise PermanentEmailError("Password reset token must not be empty")
        content = render_password_reset_email(token=token, locale=locale)
        payload = self._payload(
            to_email=email,
            subject=content.subject,
            text=content.text,
            html=content.html,
        )
        await self._post(payload, kind="password reset")

    async def send_order_confirmation(self, message: OrderConfirmationEmail) -> None:
        content = render_order_confirmation_email(message)
        payload = self._payload(
            to_email=message.email,
            subject=content.subject,
            text=content.text,
            html=content.html,
        )
        await self._post(payload, kind="order confirmation")


def build_email_provider(settings: Settings | None = None) -> EmailProvider:
    settings = settings or get_settings()
    provider = settings.normalized_email_provider

    # Automated tests must never perform real delivery. Brevo can still be
    # exercised directly in tests with an injected httpx.MockTransport.
    if settings.app_env.lower() == "test":
        return DevEmailProvider()

    if provider == "dev":
        if settings.is_production:
            raise RuntimeError(
                "EMAIL_PROVIDER=dev is forbidden in production; configure Brevo"
            )
        return DevEmailProvider()

    if provider == "brevo":
        sender_name, sender_email = settings.effective_email_sender
        if not settings.brevo_api_key.strip():
            raise RuntimeError("BREVO_API_KEY is required when EMAIL_PROVIDER=brevo")
        try:
            _normalized_address(sender_email)
        except EmailNotValidError as exc:
            raise RuntimeError(
                "A valid BREVO_SENDER_EMAIL (or EMAIL_FROM address) is required "
                "when EMAIL_PROVIDER=brevo"
            ) from exc
        return BrevoEmailProvider(
            api_key=settings.brevo_api_key,
            sender_email=sender_email,
            sender_name=sender_name,
            api_base_url=settings.brevo_api_base_url,
            timeout_seconds=settings.brevo_timeout_seconds,
        )

    raise RuntimeError(f"Unsupported EMAIL_PROVIDER: {provider!r}")
