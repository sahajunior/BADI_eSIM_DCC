import { expect, test as setup } from '@playwright/test';

const DEMO_PASSWORD = process.env.BADI_DEMO_PASSWORD;

const IDENTITIES = [
  { email: 'customer1@example.test', state: 'e2e/.auth/customer1.json' },
  { email: 'customer2@example.test', state: 'e2e/.auth/customer2.json' },
  { email: 'agent1@example.test', state: 'e2e/.auth/agent1.json' },
  // agent2 is reserved for the sign-out test so revocation cannot race another test's session.
  { email: 'agent2@example.test', state: 'e2e/.auth/agent2.json' },
] as const;

for (const identity of IDENTITIES) {
  setup(`authenticate ${identity.email}`, async ({ page }) => {
    if (!DEMO_PASSWORD) {
      throw new Error('BADI_DEMO_PASSWORD is required: run make env/dev before the real-stack E2E');
    }
    // One login per identity keeps the server's per-email throttle untouched.
    await page.goto('/login');
    await page.getByLabel('Email').fill(identity.email);
    await page.getByLabel('Password').fill(DEMO_PASSWORD);
    await page.getByRole('button', { name: /sign in/i }).click();
    await expect(page).not.toHaveURL(/\/login$/);
    await page.context().storageState({ path: identity.state });
  });
}
