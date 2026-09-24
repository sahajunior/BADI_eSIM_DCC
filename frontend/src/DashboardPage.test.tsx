import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Identity } from './api';
import { DashboardPage } from './DashboardPage';

function json(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init.headers },
  });
}

const staff: Identity = {
  id: 'agent-1',
  email: 'agent1@example.test',
  display_name: 'Agent One',
  role: 'AGENT',
};

afterEach(() => {
  vi.restoreAllMocks();
});

describe('DashboardPage', () => {
  it('renders counts, grouped lists, and service-level timers', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          json({
            policy_version: '2026-01-project-defaults',
            total_tickets: 7,
            open_tickets: 4,
            unassigned: 1,
            overdue: 2,
            by_status: [
              { key: 'OPEN', count: 1 },
              { key: 'IN_PROGRESS', count: 3 },
            ],
            by_priority: [{ key: 'HIGH', count: 2 }],
            first_response: { met: 3, breached: 1, pending: 3 },
            resolution: { met: 2, breached: 0, pending: 5 },
          }),
        ),
      ),
    );

    render(<DashboardPage user={staff} revision={0} />);

    expect(await screen.findByText('Open tickets')).toBeInTheDocument();
    expect(screen.getByText('Overdue')).toBeInTheDocument();
    expect(screen.getByText('In progress')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'In progress: 3' })).toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'High: 2' })).toBeInTheDocument();
    expect(screen.getByText('2', { selector: '.metric-value-alert' })).toBeInTheDocument();
    expect(screen.queryByText(/support operations/i)).not.toBeInTheDocument();
    expect(screen.getByText('First response')).toBeInTheDocument();
    expect(screen.getByText('Resolution')).toBeInTheDocument();
  });

  it('shows a retryable error state', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          json(
            { error: { code: 'service_unavailable', message: 'Service temporarily unavailable.', fields: {} } },
            { status: 503 },
          ),
        ),
      ),
    );

    render(<DashboardPage user={staff} revision={0} />);

    expect(await screen.findByText(/service temporarily unavailable/i)).toBeInTheDocument();
  });
});
