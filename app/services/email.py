import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

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


class EmailProvider(Protocol):
    """Delivery channel for transactional emails."""

    async def send_verification_code(self, *, email: str, code: str, locale: str = "en") -> None:
        ...

    async def send_password_reset(self, *, email: str, token: str, locale: str = "en") -> None:
        ...

    async def send_order_confirmation(self, message: OrderConfirmationEmail) -> None:
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


def build_email_provider() -> EmailProvider:
    # No transactional provider is configured yet; the dev provider keeps the
    # interface stable and lets tests assert delivery without a mail server.
    return DevEmailProvider()
