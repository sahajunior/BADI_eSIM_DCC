import { existsSync } from 'node:fs';
import { expect, test } from '@playwright/test';

const AGENT_STATE = 'e2e/.auth/agent1.json';

test.use({ storageState: AGENT_STATE });

test('an agent sees the service-level timers on a ticket', async ({ page }) => {
  test.skip(!existsSync(AGENT_STATE), 'run the auth setup first');

  await page.goto('/agent/tickets');
  await page.getByRole('link', { name: /eSIM installed but no internet/i }).first().click();
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();

  const sla = page.getByRole('region', { name: /service level/i });
  await expect(sla).toBeVisible();
  await expect(sla.getByText('First response')).toBeVisible();
  await expect(sla.getByText('Resolution')).toBeVisible();
  await expect(sla.getByText(/policy /i)).toBeVisible();
});
