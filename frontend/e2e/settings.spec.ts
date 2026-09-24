import { existsSync } from 'node:fs';
import { expect, test } from '@playwright/test';

const CUSTOMER_STATE = 'e2e/.auth/customer1.json';

test.use({ storageState: CUSTOMER_STATE });

test('a user can view and change their reply-email preference', async ({ page, browser }, testInfo) => {
  test.skip(!existsSync(CUSTOMER_STATE), 'run the auth setup first');
  // The preference is shared per user; run once to avoid a device race.
  test.skip(testInfo.project.name !== 'desktop', 'preference check runs once');
  const baseURL = String(testInfo.project.use.baseURL ?? 'http://127.0.0.1:8080');

  await page.goto('/settings/notifications');
  const toggle = page.getByRole('checkbox', { name: /email me about replies/i });
  await expect(toggle).toBeVisible();

  const before = await toggle.isChecked();
  const next = !before;

  const patched = page.waitForResponse(
    (response) =>
      response.url().includes('/api/v1/notification-preferences') &&
      response.request().method() === 'PATCH',
  );
  await toggle.setChecked(next);
  const response = await patched;
  expect(response.status()).toBe(200);
  expect((await response.json()).email_on_reply).toBe(next);
  await expect(toggle).toBeChecked({ checked: next });

  // A fresh context always re-reads server truth.
  const fresh = await browser.newContext({ baseURL, storageState: CUSTOMER_STATE });
  const freshPage = await fresh.newPage();
  await freshPage.goto('/settings/notifications');
  await expect(
    freshPage.getByRole('checkbox', { name: /email me about replies/i }),
  ).toBeChecked({ checked: next });
  await fresh.close();
});
