import { existsSync } from 'node:fs';
import { expect, test } from '@playwright/test';

const CUSTOMER_STATE = 'e2e/.auth/customer1.json';

test.use({ storageState: CUSTOMER_STATE });

test('live updates reconnect after a stream failure', async ({ page }) => {
  test.skip(!existsSync(CUSTOMER_STATE), 'run the auth setup first');
  test.setTimeout(60_000);

  // Force the live stream to fail, then allow it to reconnect.
  await page.route('**/api/v1/events*', (route) => route.abort());

  await page.goto('/support');
  await expect(page.getByRole('heading', { name: /my tickets/i })).toBeVisible();

  const reconnected = page.waitForRequest(
    (request) => new URL(request.url()).pathname === '/api/v1/events',
    { timeout: 20_000 },
  );
  await page.unroute('**/api/v1/events*');
  await reconnected;

  // Private data is still correct after recovery.
  await page.reload();
  await expect(page.getByRole('heading', { name: /my tickets/i })).toBeVisible();
});
