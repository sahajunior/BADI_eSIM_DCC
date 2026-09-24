import { existsSync } from 'node:fs';
import { expect, test } from '@playwright/test';

const CUSTOMER_STATE = 'e2e/.auth/customer1.json';
const OTHER_CUSTOMER_STATE = 'e2e/.auth/customer2.json';

test.use({ storageState: CUSTOMER_STATE });

test('a customer cannot read another customer ticket or staff routes', async (
  { page, browser },
  testInfo,
) => {
  test.skip(
    !existsSync(CUSTOMER_STATE) || !existsSync(OTHER_CUSTOMER_STATE),
    'run the auth setup first',
  );
  const baseURL = String(testInfo.project.use.baseURL ?? 'http://127.0.0.1:8080');

  // Capture the other customer's ticket identifier from their own list.
  async function otherCustomerTicketHref(): Promise<string | null> {
    const otherContext = await browser.newContext({
      baseURL,
      storageState: OTHER_CUSTOMER_STATE,
    });
    try {
      const otherPage = await otherContext.newPage();
      await otherPage.goto('/support');
      return await otherPage
        .getByRole('link', { name: /activation qr code already used/i })
        .getAttribute('href');
    } finally {
      await otherContext.close();
    }
  }
  const otherTicketHref = await otherCustomerTicketHref();
  expect(otherTicketHref).toBeTruthy();

  // Customer one must not see or load the other customer's ticket.
  await page.goto('/support');
  await expect(page).toHaveURL(/\/support$/);
  await expect(page.getByText(/activation qr code already used/i)).toHaveCount(0);

  await page.goto(otherTicketHref as string);
  await expect(page.getByText(/resource not found/i)).toBeVisible();

  // Staff-only routes are refused without staff authorization.
  await page.goto('/agent/tickets');
  await expect(page.getByRole('heading', { name: /staff access required/i })).toBeVisible();
});
