"""Prepare private dev configuration; preserve existing values and isolate tests."""

import secrets
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

root = Path(__file__).resolve().parents[1]
target = root / ".env"
if target.exists():
    original = target.read_text()
    content = original
    values = dict(
        line.split("=", 1)
        for line in original.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    additions = {
        "BADI_AUTH_SECRET": secrets.token_hex(32),
        "BADI_DEMO_PASSWORD": secrets.token_urlsafe(24),
        "TEST_POSTGRES_PORT": "55433",
        "BADI_APP_DB_PASSWORD": secrets.token_hex(24),
    }
    for key, value in additions.items():
        if key not in values:
            content = content.rstrip("\n") + f"\n{key}={value}\n"
    # Migrate only the known Phase 0 unsafe shared test URL. Preserve custom URLs;
    # the integration fixture independently refuses non-test databases.
    if values.get("BADI_DATABASE_URL") and values.get("BADI_TEST_DATABASE_URL") == values.get(
        "BADI_DATABASE_URL"
    ):
        port = values.get("TEST_POSTGRES_PORT", "55433")
        content = content.replace(
            f"BADI_TEST_DATABASE_URL={values['BADI_DATABASE_URL']}",
            f"BADI_TEST_DATABASE_URL=postgresql+asyncpg://badi:badi-tests-only@127.0.0.1:{port}/badi_test",
        )
    # Upgrade the generated local admin DSN to a separate application login. Keep
    # the original privileged DSN for the explicit migration command, never API.
    if "BADI_MIGRATION_DATABASE_URL" not in values and values.get("BADI_DATABASE_URL"):
        old_url = values["BADI_DATABASE_URL"]
        parsed = urlsplit(old_url)
        if (
            parsed.scheme == "postgresql+asyncpg"
            and parsed.hostname in {"127.0.0.1", "localhost"}
            and parsed.username == values.get("POSTGRES_USER", "badi")
        ):
            app_password = values.get("BADI_APP_DB_PASSWORD", additions["BADI_APP_DB_PASSWORD"])
            authority = f"badi_app:{quote(app_password, safe='')}@{parsed.hostname}"
            if parsed.port:
                authority += f":{parsed.port}"
            app_url = urlunsplit(parsed._replace(netloc=authority))
            content = content.replace(
                f"BADI_DATABASE_URL={old_url}", f"BADI_DATABASE_URL={app_url}"
            )
            content = content.rstrip("\n") + f"\nBADI_MIGRATION_DATABASE_URL={old_url}\n"
    if content != original:
        target.chmod(0o600)
        target.write_text(content)
        print("Added missing development settings; moved legacy shared tests to isolated DB.")
    else:
        print("Keeping existing .env unchanged.")
else:
    content = (
        (root / ".env.example")
        .read_text()
        .replace("change-me-local-only", secrets.token_hex(24))
        .replace("generate-auth-secret", secrets.token_hex(32))
        .replace("generate-demo-password", secrets.token_urlsafe(24))
        .replace("generate-app-db-password", secrets.token_hex(24))
    )
    # Exclusive create avoids overwriting another process's configuration.
    with target.open("x") as output:
        target.chmod(0o600)
        output.write(content)
    print("Created private .env with a random local database password.")
