"""Regression tests for non-destructive development setup (no Docker required)."""

import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class SetupEnvironmentTests(unittest.TestCase):
    def test_generates_private_random_configuration_once(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            shutil.copy(source / "scripts/setup-env.py", root / "scripts/setup-env.py")
            shutil.copy(source / ".env.example", root / ".env.example")

            command = [sys.executable, str(root / "scripts/setup-env.py")]
            subprocess.run(command, check=True, capture_output=True)
            target = root / ".env"
            original = target.read_bytes()
            self.assertNotIn(b"change-me-local-only", original)
            self.assertNotIn(b"generate-auth-secret", original)
            self.assertNotIn(b"generate-demo-password", original)
            self.assertNotIn(b"generate-app-db-password", original)
            self.assertIn(b"/badi_test", original)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

            subprocess.run(command, check=True, capture_output=True)
            self.assertEqual(target.read_bytes(), original)

    def test_upgrades_phase_zero_without_rotating_existing_credentials(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            shutil.copy(source / "scripts/setup-env.py", root / "scripts/setup-env.py")
            app_url = "postgresql+asyncpg://badi:existing-secret@127.0.0.1:55432/badi"
            target = root / ".env"
            target.write_text(
                f"POSTGRES_PASSWORD=existing-secret\nBADI_DATABASE_URL={app_url}\n"
                f"BADI_TEST_DATABASE_URL={app_url}\nBADI_AUTH_SECRET=keep-existing-auth-key\n"
            )
            subprocess.run(
                [sys.executable, str(root / "scripts/setup-env.py")],
                check=True,
                capture_output=True,
            )
            result = target.read_text()
            self.assertIn("POSTGRES_PASSWORD=existing-secret", result)
            self.assertIn(f"BADI_MIGRATION_DATABASE_URL={app_url}", result)
            self.assertIn("BADI_DATABASE_URL=postgresql+asyncpg://badi_app:", result)
            self.assertIn("BADI_AUTH_SECRET=keep-existing-auth-key", result)
            self.assertIn("BADI_DEMO_PASSWORD=", result)
            self.assertIn("/badi_test", result)
            self.assertNotIn(f"BADI_TEST_DATABASE_URL={app_url}", result)


if __name__ == "__main__":
    unittest.main()
