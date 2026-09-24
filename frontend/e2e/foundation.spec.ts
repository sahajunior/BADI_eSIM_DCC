import { expect, test } from '@playwright/test';

test('foundation page proves ready API/database health on desktop and mobile', async ({ page, request }, testInfo) => {
  const pageErrors: string[] = [];
  page.on('pageerror', (error) => pageErrors.push(error.message));
  page.on('console', (message) => {
    if (message.type() === 'error') {
      pageErrors.push(message.text());
    }
  });

  const healthResponse = await request.get('/api/v1/health/ready');
  expect(healthResponse.status()).toBe(200);
  await expect(healthResponse).toBeOK();
  await expect(healthResponse.json()).resolves.toEqual({ status: 'ready', database: 'ok' });

  const docsResponse = await request.get('/api/v1/docs');
  await expect(docsResponse).toBeOK();

  await page.goto('/health');

  await expect(page.getByRole('heading', { name: /a reliable starting point/i })).toBeVisible();
  await expect(page.getByText(/^ready$/i)).toBeVisible();
  await expect(page.getByText(/postgresql ok/i)).toBeVisible();

  const readyAfterRefresh = page.waitForResponse((response) =>
    response.url().includes('/api/v1/health/ready') && response.status() === 200,
  );
  await page.getByRole('button', { name: /check again/i }).click();
  await readyAfterRefresh;
  await expect(page.getByText(/^ready$/i)).toBeVisible();
  await expect(page.getByRole('button', { name: /check again/i })).toBeEnabled();

  await expect(page.getByRole('link', { name: /api documentation/i })).toHaveAttribute(
    'href',
    '/api/v1/docs',
  );

  const hasHorizontalOverflow = await page.evaluate(() => {
    const root = document.documentElement;
    return root.scrollWidth > root.clientWidth + 1;
  });
  expect(hasHorizontalOverflow).toBe(false);
  expect(pageErrors).toEqual([]);

  await page.screenshot({
    path: testInfo.outputPath(`${testInfo.project.name}-foundation-ready.png`),
    fullPage: true,
  });
});
