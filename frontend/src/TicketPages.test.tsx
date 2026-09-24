import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, clearSession, type AgentTicket, type CustomerTicket, type Identity } from './api';
import { TicketCreate, TicketList } from './TicketPages';

function json(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init.headers },
  });
}

const customer: Identity = {
  id: 'cust-1',
  email: 'customer1@example.test',
  display_name: 'Customer One',
  role: 'CUSTOMER',
};
const staff: Identity = {
  id: 'agent-1',
  email: 'agent1@example.test',
  display_name: 'Agent One',
  role: 'AGENT',
};

const customerTicket: CustomerTicket = {
  id: '11111111-1111-1111-1111-111111111111',
  ticket_number: 'BD-1001',
  subject: 'eSIM installed but no internet',
  description: 'No data connection after install.',
  category: 'CONNECTIVITY',
  priority: 'HIGH',
  status: 'OPEN',
  order_id: null,
  created_at: '2026-09-22T10:00:00Z',
  public_updated_at: '2026-09-22T10:00:00Z',
};

const staffTicket: AgentTicket = {
  ...customerTicket,
  customer_id: 'cust-1',
  customer_email_snapshot: 'customer1@example.test',
  created_by_id: 'cust-1',
  assigned_agent_id: 'agent-1',
  version: 1,
  updated_at: '2026-09-22T10:00:00Z',
};

beforeEach(() => {
  window.scrollTo = vi.fn();
});

afterEach(() => {
  clearSession();
  vi.restoreAllMocks();
});

describe('TicketList', () => {
  it('renders a customer ticket row linking to detail without staff columns', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(json({ items: [customerTicket], next_cursor: null }))),
    );

    render(
      <TicketList
        user={customer}
        base="/support"
        detailBase="/support/tickets"
        location="/support"
        revision={0}
      />,
    );

    expect(await screen.findByText('BD-1001')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /eSIM installed but no internet/i })).toHaveAttribute(
      'href',
      '/support/tickets/11111111-1111-1111-1111-111111111111',
    );
    expect(screen.getByRole('columnheader', { name: '#' })).toBeInTheDocument();
    expect(screen.queryByRole('columnheader', { name: /customer/i })).not.toBeInTheDocument();
  });

  it('keeps the current tickets visible while a live-update refresh is pending', async () => {
    let resolveRefresh!: (response: Response) => void;
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(json({ items: [customerTicket], next_cursor: null }))
      .mockImplementationOnce(
        () => new Promise<Response>((resolve) => { resolveRefresh = resolve; }),
      );
    vi.stubGlobal('fetch', fetchMock);

    const { rerender } = render(
      <TicketList
        user={customer}
        base="/support"
        detailBase="/support/tickets"
        location="/support"
        revision={0}
      />,
    );
    expect(await screen.findByText('BD-1001')).toBeInTheDocument();

    rerender(
      <TicketList
        user={customer}
        base="/support"
        detailBase="/support/tickets"
        location="/support"
        revision={1}
      />,
    );

    expect(screen.getByText('BD-1001')).toBeInTheDocument();
    expect(screen.queryByText('Loading tickets…')).not.toBeInTheDocument();

    resolveRefresh(json({
      items: [{ ...customerTicket, subject: 'Updated ticket subject' }],
      next_cursor: null,
    }));
    expect(await screen.findByText('Updated ticket subject')).toBeInTheDocument();
  });

  it('shows the staff customer and assignee columns with a resolved agent name', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.startsWith('/api/v1/agents')) {
          return Promise.resolve(json({ items: [{ id: 'agent-1', display_name: 'Agent One' }] }));
        }
        return Promise.resolve(json({ items: [staffTicket], next_cursor: null }));
      }),
    );

    render(
      <TicketList
        user={staff}
        base="/agent/tickets"
        detailBase="/agent/tickets"
        location="/agent/tickets"
        revision={0}
      />,
    );

    expect(await screen.findByText('customer1@example.test')).toBeInTheDocument();
    expect(await screen.findByText('Agent One')).toBeInTheDocument();
  });

  it('distinguishes unassigned staff tickets and exposes segmented queue views', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.startsWith('/api/v1/agents')) {
          return Promise.resolve(json({ items: [] }));
        }
        return Promise.resolve(
          json({ items: [{ ...staffTicket, assigned_agent_id: null }], next_cursor: null }),
        );
      }),
    );

    render(
      <TicketList
        user={staff}
        base="/agent/tickets"
        detailBase="/agent/tickets"
        location="/agent/tickets"
        revision={0}
      />,
    );

    expect(await screen.findByText('Unassigned', { selector: '.ticket-assignee' })).toHaveClass(
      'ticket-assignee-unassigned',
    );
    expect(screen.getByRole('navigation', { name: /queue views/i })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'All' })).toHaveAttribute('aria-current', 'page');
  });

  it('offers an empty state and a create action when there are no tickets', async () => {    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(json({ items: [], next_cursor: null }))),
    );

    render(
      <TicketList
        user={customer}
        base="/support"
        detailBase="/support/tickets"
        location="/support"
        revision={0}
      />,
    );

    expect(await screen.findByRole('heading', { name: /no tickets yet/i })).toBeInTheDocument();
    expect(screen.getAllByRole('link', { name: /new ticket/i })[0]).toHaveAttribute(
      'href',
      '/support/new',
    );
  });
});

describe('TicketCreate', () => {
  it('submits a customer ticket with a matching email and an idempotency key', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === '/api/v1/auth/csrf') {
        return Promise.resolve(json({ csrf_token: 'test-csrf' }));
      }
      if (url === '/api/v1/tickets' && init?.method === 'POST') {
        return Promise.resolve(json(customerTicket, { status: 201 }));
      }
      return Promise.resolve(json({ items: [] }));
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<TicketCreate user={customer} base="/support" detailBase="/support/tickets" />);

    await user.selectOptions(screen.getByLabelText('Category'), 'CONNECTIVITY');
    await user.type(screen.getByLabelText('Subject'), 'eSIM installed but no internet');
    await user.type(screen.getByLabelText(/Description/), 'No data connection after install.');
    await user.click(screen.getByRole('button', { name: /create ticket/i }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const call = fetchMock.mock.calls.find(
      ([url, init]) => String(url) === '/api/v1/tickets' && init?.method === 'POST',
    );
    expect(call).toBeDefined();
    const [url, init] = call!;
    expect(url).toBe('/api/v1/tickets');
    expect(init?.headers).toMatchObject({ 'Idempotency-Key': expect.any(String) });
    expect(JSON.parse(String(init?.body))).toMatchObject({
      customer_email: 'customer1@example.test',
      category: 'CONNECTIVITY',
      priority: 'MEDIUM',
    });
    await waitFor(() =>
      expect(window.location.pathname).toBe('/support/tickets/11111111-1111-1111-1111-111111111111'),
    );
  });

  it('blocks submission and explains missing required fields', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(() => Promise.resolve(json({ items: [] })));
    vi.stubGlobal('fetch', fetchMock);

    render(<TicketCreate user={customer} base="/support" detailBase="/support/tickets" />);

    await user.click(screen.getByRole('button', { name: /create ticket/i }));

    expect(await screen.findByText(/complete the required fields/i)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('surfaces server validation field errors', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === '/api/v1/auth/csrf') {
        return Promise.resolve(json({ csrf_token: 'test-csrf' }));
      }
      if (String(input) === '/api/v1/tickets' && init?.method === 'POST') {
        return Promise.resolve(
          json(
            {
              error: {
                code: 'validation_error',
                message: 'Invalid request.',
                fields: { 'body.subject': 'String should have at most 200 characters' },
              },
            },
            { status: 422 },
          ),
        );
      }
      return Promise.resolve(json({ items: [] }));
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<TicketCreate user={customer} base="/support" detailBase="/support/tickets" />);

    await user.selectOptions(screen.getByLabelText('Category'), 'OTHER');
    await user.type(screen.getByLabelText('Subject'), 'Something specific');
    await user.type(screen.getByLabelText(/Description/), 'Details');
    await user.click(screen.getByRole('button', { name: /create ticket/i }));

    expect(await screen.findByText(/invalid request/i)).toBeInTheDocument();
    expect(screen.getByText(/subject: string should have at most/i)).toBeInTheDocument();
  });

  it('requires staff to select a customer before creating', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(() => Promise.resolve(json({ items: [] })));
    vi.stubGlobal('fetch', fetchMock);

    render(<TicketCreate user={staff} base="/agent/tickets" detailBase="/agent/tickets" />);

    await user.selectOptions(screen.getByLabelText('Category'), 'ORDER');
    await user.type(screen.getByLabelText('Subject'), 'Order question');
    await user.type(screen.getByLabelText(/Description/), 'Please check my order.');
    await user.click(screen.getByRole('button', { name: /create ticket/i }));

    expect(await screen.findByText(/select the customer/i)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('ApiError field rendering contract', () => {
  it('keeps field keys available for the shared error notice', () => {
    const error = new ApiError(422, 'validation_error', 'Invalid request.', {
      'body.email': 'valid email required',
    });
    expect(error.fields).toEqual({ 'body.email': 'valid email required' });
  });
});

describe('TicketList unread markers', () => {
  it('shows an accessible unread indicator for a ticket with unread activity', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(json({ items: [{ ...customerTicket, unread_count: 2 }], next_cursor: null })),
      ),
    );

    render(
      <TicketList
        user={customer}
        base="/support"
        detailBase="/support/tickets"
        location="/support"
        revision={0}
      />,
    );

    expect(await screen.findByText('2 unread')).toBeInTheDocument();
  });
});
