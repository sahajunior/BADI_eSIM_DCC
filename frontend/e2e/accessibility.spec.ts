import { existsSync } from 'node:fs';
import { expect, test } from '@playwright/test';

const CUSTOMER_STATE = 'e2e/.auth/customer1.json';

test('sign-in form is keyboard operable with labelled controls', async ({ page }) => {
  await page.goto('/login');
  await expect(page.getByRole('heading', { name: /welcome back/i })).toBeVisible();

  const email = page.getByLabel('Email');
  const password = page.getByLabel('Password');
  const submit = page.getByRole('button', { name: /sign in/i });
  await expect(email).toBeVisible();
  await expect(password).toBeVisible();
  await expect(submit).toBeVisible();

  await email.focus();
  await expect(email).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(password).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(submit).toBeFocused();
});

test.describe('authenticated workspace', () => {
  test.use({ storageState: CUSTOMER_STATE });

  test('create and queue controls expose labels and keyboard order', async ({ page }) => {
    test.skip(!existsSync(CUSTOMER_STATE), 'run the auth setup first');

    await page.goto('/support/new');
    const category = page.getByLabel('Category');
    const subject = page.getByLabel('Subject');
    const description = page.getByLabel(/Description/);
    const priority = page.getByLabel('Priority');
    await expect(category).toBeVisible();
    await expect(subject).toBeVisible();
    await expect(description).toBeVisible();
    await expect(priority).toBeVisible();

    await category.focus();
    await expect(category).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(subject).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(description).toBeFocused();

    await page.goto('/support');
    await expect(page.getByLabel('Search')).toBeVisible();
    await expect(page.getByLabel('Status')).toBeVisible();
    await expect(page.getByLabel('Priority')).toBeVisible();
    await expect(page.getByLabel('Category')).toBeVisible();
  });
});
