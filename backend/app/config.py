from functools import lru_cache
from typing import Literal, cast
from urllib.parse import urlparse

from pydantic import Field, PostgresDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """Application settings loaded from explicit BADI_* environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="BADI_",
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    database_url: PostgresDsn = Field(
        default=cast(PostgresDsn, "postgresql+asyncpg://badi@localhost:55432/badi"),
        description="Async PostgreSQL URL. Must use the postgresql+asyncpg scheme.",
    )
    environment: Environment = "development"

    auth_secret: SecretStr = Field(
        min_length=32,
        description="Secret key used for signed auth and CSRF tokens. Must be supplied explicitly.",
    )
    allowed_origins: list[str] = Field(
        default_factory=lambda: [
            "http://127.0.0.1:8080",
            "http://127.0.0.1:5173",
            "http://127.0.0.1:8000",
        ],
        description="Browser origins allowed to use cookie-authenticated endpoints.",
    )
    cookie_secure: bool = Field(
        default=False,
        description=(
            "Set the auth session cookie Secure flag. Defaults false for local development."
        ),
    )
    session_ttl_seconds: int = Field(
        default=28_800,
        ge=300,
        le=60 * 60 * 24 * 30,
        description="Authenticated session lifetime in seconds.",
    )
    preauth_ttl_seconds: int = Field(
        default=900,
        ge=60,
        le=60 * 60,
        description="Anonymous CSRF bootstrap session lifetime in seconds.",
    )

    sse_heartbeat_seconds: float = Field(default=15.0, ge=0.05, le=15.0)
    sse_queue_size: int = Field(default=64, ge=1, le=256)
    sse_max_per_user: int = Field(default=4, ge=1, le=10)
    sse_max_connections: int = Field(default=100, ge=1, le=1000)

    log_level: LogLevel = Field(
        default="INFO",
        description="Structured JSON log level for the API and worker.",
    )
    worker_lag_log_seconds: float = Field(
        default=15.0,
        ge=1.0,
        le=300.0,
        description="Cadence for reporting outbox backlog/lag from the worker.",
    )

    attachment_dir: str = Field(
        default="var/attachments",
        description="Private directory for staged and stored attachment objects.",
    )
    attachment_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1, le=50 * 1024 * 1024)
    attachment_ttl_seconds: int = Field(default=24 * 60 * 60, ge=60, le=7 * 24 * 60 * 60)
    attachment_scanning: Literal["enabled", "disabled"] = Field(
        default="enabled",
        description="Attachment scanning is fail-closed when not 'enabled'.",
    )

    email_backend: Literal["disabled", "console", "smtp"] = Field(
        default="console",
        description="Reply-notification delivery: console logs a redacted line; disabled skips.",
    )
    email_from: str = Field(default="support@example.test", max_length=254)
    smtp_host: str = Field(default="127.0.0.1", max_length=255)
    smtp_port: int = Field(default=1025, ge=1, le=65535)
    public_base_url: str = Field(default="http://127.0.0.1:8080", max_length=255)
    notification_max_attempts: int = Field(default=8, ge=1, le=20)

    @field_validator("database_url", mode="after")
    @classmethod
    def require_asyncpg_scheme(cls, value: PostgresDsn) -> PostgresDsn:
        if value.scheme != "postgresql+asyncpg":
            raise ValueError("BADI_DATABASE_URL must use postgresql+asyncpg://")
        return value

    @field_validator("allowed_origins", mode="after")
    @classmethod
    def normalize_allowed_origins(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for origin in value:
            parsed = urlparse(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError("BADI_ALLOWED_ORIGINS entries must be explicit http(s) origins")
            if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
                raise ValueError(
                    "BADI_ALLOWED_ORIGINS entries must not include paths or query strings"
                )
            normalized_origin = f"{parsed.scheme}://{parsed.netloc}"
            if normalized_origin not in normalized:
                normalized.append(normalized_origin)
        if not normalized:
            raise ValueError("BADI_ALLOWED_ORIGINS must not be empty")
        return normalized

    @field_validator("environment", mode="after")
    @classmethod
    def fail_closed_for_production(cls, value: Environment) -> Environment:
        if value == "production":
            raise ValueError(
                "production is disabled until deployment security hardening is configured"
            )
        return value

    @property
    def sqlalchemy_database_url(self) -> str:
        return str(self.database_url).rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
