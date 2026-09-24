import pytest
from pydantic import ValidationError

from app.config import Settings


def test_settings_default_database_url_is_local_and_passwordless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BADI_DATABASE_URL", raising=False)
    monkeypatch.delenv("BADI_ENVIRONMENT", raising=False)
    settings = Settings(_env_file=None, environment="development")

    assert settings.sqlalchemy_database_url == "postgresql+asyncpg://badi@localhost:55432/badi"
    assert "password" not in settings.sqlalchemy_database_url
    assert "devpassword" not in settings.sqlalchemy_database_url


def test_settings_accept_development_asyncpg_url() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://user:pass@localhost:5432/badi",
        environment="development",
    )

    assert settings.sqlalchemy_database_url == "postgresql+asyncpg://user:pass@localhost:5432/badi"
    assert settings.environment == "development"


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://user:pass@localhost:5432/badi",
        "sqlite+aiosqlite:///tmp.db",
        "not-a-url",
    ],
)
def test_settings_reject_invalid_database_url(url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(database_url=url, environment="development")


def test_settings_fail_closed_for_production() -> None:
    with pytest.raises(ValidationError, match="production is disabled"):
        Settings(
            database_url="postgresql+asyncpg://user:pass@localhost:5432/badi",
            environment="production",
        )


def test_settings_reject_unknown_environment() -> None:
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql+asyncpg://user:pass@localhost:5432/badi",
            environment="staging",
        )


def test_settings_validation_errors_hide_malformed_secret_input() -> None:
    with pytest.raises(ValidationError) as exc_info:
        Settings(database_url="postgresql://user:super-secret@localhost:5432/badi")

    error_text = str(exc_info.value)
    assert "super-secret" not in error_text
    assert "input_value" not in error_text
