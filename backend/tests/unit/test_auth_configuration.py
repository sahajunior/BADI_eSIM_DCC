import pytest
from pydantic import ValidationError

from app.auth.security import csrf_matches
from app.config import Settings
from app.provision import canonical_email


@pytest.mark.parametrize(
    "origins",
    [[], ["*"], ["https://example.test/path"], ["https://user:pass@example.test"]],
)
def test_unsafe_origin_configuration_rejected(origins: list[str]) -> None:
    with pytest.raises(ValidationError):
        Settings(allowed_origins=origins)


def test_short_auth_secret_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(auth_secret="short")


def test_csrf_non_ascii_is_rejected_without_exception() -> None:
    assert not csrf_matches(Settings(), "arbitrary-token", "é" * 64)


def test_demo_email_is_normalized_consistently() -> None:
    assert canonical_email(" Customer1@Example.Test ") == "customer1@example.test"
