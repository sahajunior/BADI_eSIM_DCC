import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { HealthPage } from './HealthPage';
import { fetchHealth, HEALTH_ENDPOINT } from './health';

function jsonResponse(body: unknown, init: ResponseInit): Response {
  return new Response(JSON.stringify(body), {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init.headers },
  });
}

describe('App service health foundation', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('shows an honest loading state before the API answers', () => {
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => undefined)));

    render(<HealthPage />);

    expect(screen.getByRole('heading', { name: /service health/i })).toBeInTheDocument();
    expect(screen.getByText(/checking services/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /checking/i })).toBeDisabled();
    expect(screen.getByRole('link', { name: /api docs/i })).toHaveAttribute(
      'href',
      '/api/v1/docs',
    );
    expect(
      screen.queryByText(/independent ai-assisted support ticketing project foundation/i),
    ).not.toBeInTheDocument();
  });

  it('renders ready only after the backend confirms API and database readiness', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ status: 'ready', database: 'ok' }, { status: 200 }),
    );
    vi.stubGlobal('fetch', fetchMock);

    render(<HealthPage />);

    expect(await screen.findByText(/^ready$/i)).toBeInTheDocument();
    expect(screen.getByText(/postgresql ok/i)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      HEALTH_ENDPOINT,
      expect.objectContaining({ method: 'GET', headers: { Accept: 'application/json' } }),
    );
  });

  it('renders unavailable for an explicit 503 database response', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        jsonResponse(
          { status: 'unavailable', database: 'unavailable' },
          { status: 503 },
        ),
      ),
    );

    render(<HealthPage />);

    expect(await screen.findByText(/services unavailable/i)).toBeInTheDocument();
    expect(screen.getByText(/database is not ready/i)).toBeInTheDocument();
    expect(screen.getByText(/^reachable$/i)).toBeInTheDocument();
    expect(screen.getByText(/^unavailable$/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /check again/i })).toBeEnabled();
  });

  it('retries after a network failure and updates to ready', async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new TypeError('network down'))
      .mockResolvedValueOnce(jsonResponse({ status: 'ready', database: 'ok' }, { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();

    render(<HealthPage />);

    expect(await screen.findByText(/could not reach the api/i)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /check again/i }));

    expect(await screen.findByText(/^ready$/i)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('renders an embedded staff variant without the public back link', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(jsonResponse({ status: 'ready', database: 'ok' }, { status: 200 })),
    );

    const { container } = render(<HealthPage embedded />);

    expect(await screen.findByText(/^ready$/i)).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /back to support/i })).not.toBeInTheDocument();
    expect(container.querySelector('.status-page-embedded')).toBeInTheDocument();
  });
});

describe('fetchHealth', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('aborts a slow request after the bounded timeout', async () => {
    const fetcher = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => {
          reject(new DOMException('Aborted', 'AbortError'));
        });
      });
    });

    await expect(fetchHealth(fetcher, 1)).resolves.toEqual({
      phase: 'unavailable',
      reason: 'The health check timed out. Please try again.',
    });
    expect(fetcher.mock.calls[0]?.[1]?.signal?.aborted).toBe(true);
  });
});
