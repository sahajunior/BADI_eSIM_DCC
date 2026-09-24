import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { clearSession } from './api';
import { SettingsNotificationsPage } from './SettingsNotificationsPage';

function json(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init.headers },
  });
}

afterEach(() => {
  clearSession();
  vi.restoreAllMocks();
});

describe('SettingsNotificationsPage', () => {
  it('loads the preference and persists a toggle', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === '/api/v1/auth/csrf') {
        return Promise.resolve(json({ csrf_token: 'csrf' }));
      }
      if (String(input) === '/api/v1/notification-preferences' && (init?.method ?? 'GET') === 'GET') {
        return Promise.resolve(json({ email_on_reply: true }));
      }
      return Promise.resolve(json({ email_on_reply: false }));
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsNotificationsPage />);

    const toggle = await screen.findByRole('checkbox', { name: /email me about replies/i });
    expect(toggle).toBeChecked();
    await user.click(toggle);
    expect(await screen.findByRole('checkbox', { name: /email me about replies/i })).not.toBeChecked();
  });
});
