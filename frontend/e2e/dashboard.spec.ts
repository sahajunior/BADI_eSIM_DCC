import { existsSync } from 'node:fs';
import { expect, test } from '@playwright/test';

const AGENT_STATE = 'e2e/.auth/agent1.json';

test.use({ storageState: AGENT_STATE });

test('an agent can open the operational dashboard', async ({ page }) => {
  test.skip(!existsSync(AGENT_STATE), 'run the auth setup first');

  await page.goto('/agent/dashboard');
  await expect(page.getByRole('heading', { name: 'Dashboard', exact: true })).toBeVisible();
  await expect(page.getByText('Open tickets')).toBeVisible();
  await expect(page.getByText('Unassigned')).toBeVisible();
  await expect(page.getByText('Service levels')).toBeVisible();
});
