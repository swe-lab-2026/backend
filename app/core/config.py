from email.utils import parseaddr
from functools import lru_cache

from email_validator import EmailNotValidError, validate_email
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Placeholder-looking values that must never be used in production. Startup
# refuses to run with them so a bad .env cannot be deployed by accident.
_INSECURE_SECRET_MARKERS = ("change-me", "replace-me", "example", "your-", "xxx")
_EMAIL_PROVIDERS = {"dev", "brevo"}


def _split_email_from(value: str) -> tuple[str, str]:
    """Split ``EMAIL_FROM`` into ``(display_name, address)``.

    ``email.utils.parseaddr`` accepts both ``Name <a@b>`` and a bare address.
    Invalid/empty values return an empty address so validation can report it.
    """
    name, address = parseaddr(value or "")
    return name.strip(), address.strip()


def _valid_email(value: str) -> bool:
    try:
        validate_email(value, check_deliverability=False)
    except EmailNotValidError:
        return False
    return True


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = "development"
    app_debug: bool = False
    app_name: str = "BiletFlow API"
    app_version: str = "0.1.0"

    api_prefix: str = "/api/v1"
    cors_origins: str = ""

    database_url: str = "postgresql+asyncpg://biletflow:biletflow@localhost:5432/biletflow"
    redis_url: str = ""
    apply_schema_on_startup: bool = True
    schema_files: list[str] = [
        "sql/migrate_001.sql",
        "sql/migrate_002_auth.sql",
        "sql/migrate_003_events.sql",
        "sql/migrate_004_password_reset.sql",
        "sql/migrate_005_tickets.sql",
        "sql/migrate_006_paid_sales.sql",
    ]

    # --- Tickets / checkout ------------------------------------------------
    # Used to sign QR admission payloads. When empty, the access-token secret
    # is used so existing deployments keep working after an upgrade.
    ticket_qr_secret: str = Field(default="")
    public_base_url: str = ""
    order_reservation_ttl_minutes: int = Field(default=10, ge=1, le=1440)
    reservation_cleanup_interval_seconds: int = Field(default=60, ge=1, le=3600)
    reservation_cleanup_enabled: bool = True
    notification_retry_interval_seconds: int = Field(default=60, ge=1, le=3600)
    notification_max_attempts: int = Field(default=5, ge=1, le=50)
    notification_retry_base_seconds: int = Field(default=30, ge=0, le=3600)
    notification_retry_max_seconds: int = Field(default=3600, ge=1, le=86400)

    # --- Auth / JWT -------------------------------------------------------
    # Access and refresh tokens use different secrets, taken only from the
    # environment. Both are mandatory and must be at least 32 random bytes.
    jwt_access_secret: str = Field(default="")
    jwt_refresh_secret: str = Field(default="")
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "biletflow-api"
    jwt_audience: str = "biletflow-clients"
    access_token_ttl_minutes: int = Field(default=15, ge=1, le=1440)
    refresh_token_ttl_days: int = Field(default=30, ge=1, le=365)

    # --- Email verification ----------------------------------------------
    email_code_ttl_minutes: int = Field(default=10, ge=1, le=60)
    email_code_max_attempts: int = Field(default=5, ge=1, le=20)
    email_code_resend_cooldown_seconds: int = Field(default=60, ge=10, le=3600)
    email_code_max_per_hour: int = Field(default=5, ge=1, le=100)
    email_from: str = "BiletFlow <no-reply@biletflow.local>"

    # --- Transactional email provider -------------------------------------
    # dev  -> logs messages, never reaches a mail server (local/test only)
    # brevo -> Brevo transactional email REST API
    email_provider: str = "dev"
    brevo_api_key: str = Field(default="")
    # Optional explicit sender. When empty, the address/name from EMAIL_FROM
    # are used, so existing deployments do not need two sender settings.
    brevo_sender_email: str = Field(default="")
    brevo_sender_name: str = "BiletFlow"
    brevo_api_base_url: str = "https://api.brevo.com"
    brevo_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    # --- Login brute-force protection --------------------------------------
    login_max_attempts: int = Field(default=5, ge=1, le=100)
    login_lockout_minutes: int = Field(default=15, ge=1, le=1440)

    # --- Password reset ------------------------------------------------------
    password_reset_ttl_minutes: int = Field(default=30, ge=5, le=120)

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def effective_ticket_qr_secret(self) -> str:
        return self.ticket_qr_secret or self.jwt_access_secret

    @property
    def effective_email_sender(self) -> tuple[str, str]:
        """Resolve the Brevo sender as ``(display_name, address)``.

        BREVO_SENDER_EMAIL / BREVO_SENDER_NAME take precedence. When they are
        empty the existing EMAIL_FROM value is reused, avoiding a second,
        conflicting sender configuration.
        """
        from_name, from_address = _split_email_from(self.email_from)
        sender_email = self.brevo_sender_email.strip() or from_address
        sender_name = self.brevo_sender_name.strip() or from_name or "BiletFlow"
        return sender_name, sender_email

    @property
    def normalized_email_provider(self) -> str:
        return self.email_provider.strip().lower()

    @property
    def public_api_base(self) -> str:
        return f"{self.public_base_url.rstrip('/')}{self.api_prefix}"

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @staticmethod
    def _is_insecure_secret(value: str) -> bool:
        lowered = value.lower()
        return any(marker in lowered for marker in _INSECURE_SECRET_MARKERS)

    @model_validator(mode="after")
    def validate_secrets(self) -> "Settings":
        for field in ("jwt_access_secret", "jwt_refresh_secret"):
            value = getattr(self, field)
            if len(value) < 32:
                raise ValueError(
                    f"{field.upper()} is required and must be at least 32 characters long"
                )
            if self._is_insecure_secret(value):
                raise ValueError(
                    f"{field.upper()} looks like a placeholder; generate a real random secret"
                )
        if self.jwt_access_secret == self.jwt_refresh_secret:
            raise ValueError("JWT_ACCESS_SECRET and JWT_REFRESH_SECRET must be different")

        provider = self.normalized_email_provider
        if provider not in _EMAIL_PROVIDERS:
            raise ValueError(
                f"EMAIL_PROVIDER must be one of {sorted(_EMAIL_PROVIDERS)}, got {provider!r}"
            )

        # Production must use a real delivery provider. A developer-only
        # provider would silently drop transactional mail, so refuse startup.
        if self.is_production and provider != "brevo":
            raise ValueError("EMAIL_PROVIDER=brevo is required when APP_ENV=production")
        if provider == "brevo" and self.app_env.lower() != "test":
            if not self.brevo_api_key.strip():
                raise ValueError("BREVO_API_KEY is required when EMAIL_PROVIDER=brevo")
            sender_name, sender_email = self.effective_email_sender
            if not sender_email or not _valid_email(sender_email):
                raise ValueError(
                    "A valid BREVO_SENDER_EMAIL (or a valid EMAIL_FROM address) is "
                    "required when EMAIL_PROVIDER=brevo"
                )
            if not sender_name:
                raise ValueError("BREVO_SENDER_NAME must not be blank")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()