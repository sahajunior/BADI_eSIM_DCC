import { existsSync } from 'node:fs';
import { expect, test } from '@playwright/test';

const CUSTOMER_STATE = 'e2e/.auth/customer1.json';
const AGENT_STATE = 'e2e/.auth/agent1.json';

test.use({ storageState: CUSTOMER_STATE });

test('customer and agent complete a live lifecycle with internal-note privacy', async (
  { page, browser },
  testInfo,
) => {
  test.skip(!existsSync(CUSTOMER_STATE) || !existsSync(AGENT_STATE), 'run the auth setup first');
  test.setTimeout(120_000);
  const baseURL = String(testInfo.project.use.baseURL ?? 'http://127.0.0.1:8080');

  const agentContext = await browser.newContext({ baseURL, storageState: AGENT_STATE });
  const agentPage = await agentContext.newPage();
  try {
    const stamp = Date.now();
    const subject = `E2E connectivity ${stamp}`;
    const agentReply = `We are checking the network ${stamp}`;
    const customerReply = `Standing by, thanks ${stamp}`;
    const internalSecret = `internal-provider-token-${stamp}`;

    // Customer creates a ticket.
    await page.goto('/support');
    await page.getByRole('link', { name: /new ticket/i }).first().click();
    await expect(page).toHaveURL(/\/support\/new$/);
    await page.getByLabel('Category').selectOption('CONNECTIVITY');
    await page.getByLabel('Subject').fill(subject);
    await page.getByLabel(/Description/).fill('End-to-end live lifecycle check.');
    await page.getByRole('button', { name: /create ticket/i }).click();
    await expect(page).toHaveURL(/\/support\/tickets\/[0-9a-f-]{36}$/);
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(subject);

    // Agent finds the ticket, assigns it, and changes status.
    await agentPage.goto('/agent/tickets');
    await agentPage.getByRole('link', { name: new RegExp(subject) }).click();
    await expect(agentPage.getByRole('heading', { level: 1 })).toHaveText(subject);
    await agentPage.getByLabel('Assignee').selectOption({ label: 'Agent One' });
    await agentPage.getByLabel('Status').selectOption('IN_PROGRESS');
    await agentPage.getByRole('button', { name: /save changes/i }).click();
    await expect(
      agentPage.locator('.ticket-header .badge').filter({ hasText: /^In progress$/ }),
    ).toBeVisible();

    // Agent replies publicly; the customer sees it without refreshing.
    await agentPage.getByLabel('Public reply').fill(agentReply);
    await agentPage.getByRole('button', { name: /send reply/i }).click();
    await expect(agentPage.getByText(agentReply)).toBeVisible();
    await expect(page.getByText(agentReply)).toBeVisible({ timeout: 8_000 });

    // Customer replies publicly; the agent sees it without refreshing.
    await page.getByLabel('Public reply').fill(customerReply);
    await page.getByRole('button', { name: /send reply/i }).click();
    await expect(page.getByText(customerReply)).toBeVisible();
    await expect(agentPage.getByText(customerReply)).toBeVisible({ timeout: 8_000 });

    // Internal note stays agent-only, including in the customer's network payload.
    const customerPayloads: string[] = [];
    page.on('response', async (response) => {
      if (response.url().includes('/messages')) {
        customerPayloads.push(await response.text().catch(() => ''));
      }
    });
    await agentPage.getByRole('tab', { name: /internal note/i }).click();
    await agentPage.getByLabel('Internal note').fill(internalSecret);
    await agentPage.getByRole('button', { name: /add note/i }).click();
    await expect(agentPage.getByText(internalSecret)).toBeVisible();

    await page.waitForTimeout(1_500);
    await expect(page.getByText(internalSecret)).toHaveCount(0);
    await page.reload();
    await expect(page.getByText(customerReply)).toBeVisible();
    expect(customerPayloads.join('\n')).not.toContain(internalSecret);

    // The customer page was never offered internal tooling.
    await expect(page.getByRole('tab', { name: /internal note/i })).toHaveCount(0);

    // Search filters the customer's own list (regression for the number/subject search).
    await page.goto('/support');
    await page.getByLabel('Search').fill(String(stamp));
    await expect(page).toHaveURL(new RegExp(`query=${stamp}`));
    await expect(page.getByRole('link', { name: new RegExp(subject) })).toBeVisible();
  } finally {
    await agentContext.close();
  }
});
