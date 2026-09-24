import { existsSync } from 'node:fs';
import { expect, test } from '@playwright/test';

const CUSTOMER_STATE = 'e2e/.auth/customer1.json';

test.use({ storageState: CUSTOMER_STATE });

test('a customer sees read-only order context for their own order', async ({ page }) => {
  test.skip(!existsSync(CUSTOMER_STATE), 'run the auth setup first');

  await page.goto('/support');
  await page.getByRole('link', { name: /eSIM installed but no internet/i }).first().click();
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();

  const order = page.getByRole('region', { name: /order context/i });
  await expect(order).toBeVisible();
  await expect(order.getByText('Turkey')).toBeVisible();
  await expect(order.getByText('10 GB')).toBeVisible();
  await expect(order.getByText(/completed/i)).toBeVisible();
  await expect(order.getByText(/installed/i)).toBeVisible();
});
