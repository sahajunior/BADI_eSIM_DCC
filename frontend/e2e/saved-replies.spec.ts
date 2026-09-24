import { existsSync } from 'node:fs';
import { expect, test } from '@playwright/test';

const AGENT_STATE = 'e2e/.auth/agent1.json';

test.use({ storageState: AGENT_STATE });

test('an agent can insert a saved reply into the composer', async ({ page }) => {
  test.skip(!existsSync(AGENT_STATE), 'run the auth setup first');

  await page.goto('/agent/tickets');
  await page.getByRole('link', { name: /eSIM installed but no internet/i }).first().click();
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();

  const picker = page.getByRole('combobox', { name: /insert a saved reply/i });
  await expect(picker).toBeVisible();
  const optionValue = await picker.locator('option').nth(1).getAttribute('value');
  await picker.selectOption(optionValue!);

  await expect(page.getByLabel('Public reply')).toHaveValue(/./);
});
