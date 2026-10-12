import logging
from typing import Protocol

logger = logging.getLogger("app.email")


class EmailProvider(Protocol):
    """Delivery channel for verification emails.

    Production implementations (Brevo, Resend, SMTP...) implement this
    interface; the code path that mints codes does not care which one is used.
    """

    async def send_verification_code(self, *, email: str, code: str, locale: str = "en") -> None:
        ...

    async def send_password_reset(self, *, email: str, token: str, locale: str = "en") -> None:
        ...


class DevEmailProvider:
    """Safe development implementation: logs the code and never reaches a mail server.

    The last code per address is kept in memory so local developers and tests
    can read it (the console-equivalent of a mail catcher). It is not durable,
    is never used in production, and nothing in the API responses returns it.
    """

    def __init__(self) -> None:
        self._last_codes: dict[str, str] = {}
        self._last_reset_tokens: dict[str, str] = {}

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


def build_email_provider() -> EmailProvider:
    # No transactional provider is configured yet; the dev provider keeps the
    # interface stable and lets tests assert delivery without a mail server.
    return DevEmailProvider()