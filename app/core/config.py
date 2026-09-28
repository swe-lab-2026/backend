from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Placeholder-looking values that must never be used in production. Startup
# refuses to run with them so a bad .env cannot be deployed by accident.
_INSECURE_SECRET_MARKERS = ("change-me", "replace-me", "example", "your-", "xxx")


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
    schema_files: list[str] = ["sql/migrate_001.sql", "sql/migrate_002_auth.sql"]

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

    # --- Login brute-force protection --------------------------------------
    login_max_attempts: int = Field(default=5, ge=1, le=100)
    login_lockout_minutes: int = Field(default=15, ge=1, le=1440)

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

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
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()