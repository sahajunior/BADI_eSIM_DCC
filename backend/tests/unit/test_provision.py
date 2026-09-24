import pytest

from app import provision


@pytest.mark.parametrize(
    ("password", "confirmation", "error"),
    [
        ("valid-long-password", "valid-long-password", None),
        ("valid-long-password", "different-password", "passwords do not match"),
        ("short", "short", "between 12 and 1024"),
        ("x" * 1025, "x" * 1025, "between 12 and 1024"),
    ],
)
def test_provision_password_prompt(
    monkeypatch: pytest.MonkeyPatch, password: str, confirmation: str, error: str | None
) -> None:
    responses = iter([password, confirmation])
    monkeypatch.setattr("app.provision.getpass.getpass", lambda _prompt: next(responses))
    if error is None:
        assert provision.read_password() == password
    else:
        with pytest.raises(ValueError, match=error):
            provision.read_password()
