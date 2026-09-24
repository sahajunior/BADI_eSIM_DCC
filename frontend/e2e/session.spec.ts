import { existsSync } from 'node:fs';
import { expect, test } from '@playwright/test';

// Uses the dedicated agent2 session so sign-out revocation cannot invalidate a
// session that another test is sharing in parallel. Sign-out revokes that one
// session, so the check runs once rather than in both device projects.
const LOGOUT_STATE = 'e2e/.auth/agent2.json';

test.use({ storageState: LOGOUT_STATE });

test('sign out revokes the session and clears private state', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'session revocation runs once');
  test.skip(!existsSync(LOGOUT_STATE), 'run the auth setup first');

  await page.goto('/agent/tickets');
  await expect(page.getByRole('heading', { name: /support queue/i })).toBeVisible();
  await page.getByRole('button', { name: /sign out/i }).click();
  await expect(page).toHaveURL(/\/login$/);

  // The revoked session must not resurface private data, including on refresh.
  await page.goto('/agent/tickets');
  await expect(page).toHaveURL(/\/login$/);
  await page.reload();
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole('heading', { name: /support queue/i })).toHaveCount(0);
});
