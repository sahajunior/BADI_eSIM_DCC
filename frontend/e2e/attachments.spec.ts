import { existsSync } from 'node:fs';
import { expect, test } from '@playwright/test';

const AGENT_STATE = 'e2e/.auth/agent1.json';

test.use({ storageState: AGENT_STATE });

test('an agent can attach a valid file to a public reply', async ({ page }) => {
  test.skip(!existsSync(AGENT_STATE), 'run the auth setup first');

  await page.goto('/agent/tickets');
  await page.getByRole('link', { name: /eSIM installed but no internet/i }).first().click();
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();

  const png = Buffer.from(`${'89504e470d0a1a0a'}${'00'.repeat(48)}`, 'hex');
  const filename = `diagnostic-${Date.now()}.png`;
  await page.locator('input[type=file]').setInputFiles({
    name: filename,
    mimeType: 'image/png',
    buffer: png,
  });
  await page.getByLabel('Public reply').fill('Attaching the diagnostic capture.');
  await page.getByRole('button', { name: /send reply/i }).click();

  await expect(page.getByRole('link', { name: new RegExp(filename) }).first()).toBeVisible();
});
