import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { clearSession } from './api';
import type {
  AgentTicket,
  CustomerTicket,
  Identity,
  PublicMessage,
  StaffMessage,
} from './api';
import { TicketDetail } from './TicketDetail';

function json(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init.headers },
  });
}

const ticketId = '11111111-1111-1111-1111-111111111111';
const staff: Identity = {
  id: 'agent-1',
  email: 'agent1@example.test',
  display_name: 'Agent One',
  role: 'AGENT',
};
const customer: Identity = {
  id: 'cust-1',
  email: 'customer1@example.test',
  display_name: 'Customer One',
  role: 'CUSTOMER',
};

const baseTicket = {
  id: ticketId,
  ticket_number: 'BD-1001',
  subject: 'eSIM installed but no internet',
  description: 'No data connection after install.',
  category: 'CONNECTIVITY',
  priority: 'HIGH',
  status: 'OPEN',
  order_id: null,
  created_at: '2026-09-22T10:00:00Z',
  public_updated_at: '2026-09-22T10:05:00Z',
};

const customerTicket: CustomerTicket = baseTicket;

const staffTicket: AgentTicket = {
  ...baseTicket,
  customer_id: 'cust-1',
  customer_email_snapshot: 'customer1@example.test',
  created_by_id: 'cust-1',
  assigned_agent_id: 'agent-1',
  version: 1,
  updated_at: '2026-09-22T10:05:00Z',
};

const publicMessage: StaffMessage = {
  id: 'm1',
  ticket_id: ticketId,
  body: 'We are checking the network with our provider.',
  sender_type: 'SUPPORT',
  display_name: 'Support',
  created_at: '2026-09-22T10:05:00Z',
  sender_id: 'agent-1',
  sender_role_snapshot: 'AGENT',
  visibility: 'PUBLIC',
  position: 1,
};

const internalMessage: StaffMessage = {
  ...publicMessage,
  id: 'm2',
  body: 'Provider ticket raised internally.',
  visibility: 'INTERNAL',
  position: 2,
  created_at: '2026-09-22T10:06:00Z',
};

const customerPublicMessage: PublicMessage = {
  id: 'm1',
  ticket_id: ticketId,
  body: 'Any update on this?',
  sender_type: 'CUSTOMER',
  display_name: 'Customer',
  created_at: '2026-09-22T10:04:00Z',
};

beforeEach(() => {
  window.scrollTo = vi.fn();
  Element.prototype.scrollTo = vi.fn();
});

afterEach(() => {
  clearSession();
  vi.restoreAllMocks();
});

describe('TicketDetail for staff', () => {
  it('shows public and clearly-labelled internal messages and records an internal note', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? 'GET';
      if (url === '/api/v1/auth/csrf') {
        return Promise.resolve(json({ csrf_token: 'test-csrf' }));
      }
      if (url.startsWith(`/api/v1/tickets/${ticketId}/messages`) && method === 'GET') {
        return Promise.resolve(
          json({
            items: [publicMessage, internalMessage],
            older_cursor: 'm1',
            newer_cursor: 'm2',
            has_more: false,
          }),
        );
      }
      if (url === `/api/v1/tickets/${ticketId}/messages` && method === 'POST') {
        return Promise.resolve(json({ ...internalMessage, id: 'm3', body: 'Investigating now.' }, { status: 201 }));
      }
      if (url === `/api/v1/tickets/${ticketId}/events`) {
        return Promise.resolve(json({ items: [], next_cursor: null }));
      }
      if (url === '/api/v1/agents') {
        return Promise.resolve(json({ items: [{ id: 'agent-1', display_name: 'Agent One' }] }));
      }
      if (url === `/api/v1/tickets/${ticketId}` && method === 'PATCH') {
        return Promise.resolve(
          json(
            {
              error: {
                code: 'precondition_failed',
                message: 'Ticket changed. Refresh before retrying.',
                fields: {},
              },
            },
            { status: 412 },
          ),
        );
      }
      if (url === `/api/v1/tickets/${ticketId}`) {
        return Promise.resolve(json(staffTicket, { status: 200, headers: { ETag: '"t:1"' } }));
      }
      return Promise.resolve(json({}, { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<TicketDetail ticketId={ticketId} user={staff} base="/agent/tickets" revision={0} />);

    expect(
      await screen.findByText('We are checking the network with our provider.'),
    ).toBeInTheDocument();
    expect(await screen.findByText('Provider ticket raised internally.')).toBeInTheDocument();
    expect(screen.getByText('Only your support team can see this.')).toBeInTheDocument();

    await user.click(screen.getByRole('tab', { name: /internal note/i }));
    await user.type(screen.getByLabelText('Internal note'), 'Investigating now.');
    await user.click(screen.getByRole('button', { name: /add note/i }));

    await waitFor(() => {
      const post = fetchMock.mock.calls.find(
        ([url, init]) =>
          String(url) === `/api/v1/tickets/${ticketId}/messages` && init?.method === 'POST',
      );
      expect(post).toBeDefined();
    });
    const post = fetchMock.mock.calls.find(
      ([url, init]) => String(url) === `/api/v1/tickets/${ticketId}/messages` && init?.method === 'POST',
    )!;
    expect(post[1]?.headers).toMatchObject({ 'Idempotency-Key': expect.any(String) });
    expect(JSON.parse(String(post[1]?.body))).toEqual({
      body: 'Investigating now.',
      visibility: 'INTERNAL',
    });
  });

  it('surfaces a stale-edit conflict instead of overwriting metadata', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? 'GET';
      if (url === '/api/v1/auth/csrf') {
        return Promise.resolve(json({ csrf_token: 'test-csrf' }));
      }
      if (url.startsWith(`/api/v1/tickets/${ticketId}/messages`)) {
        return Promise.resolve(
          json({ items: [publicMessage], older_cursor: null, newer_cursor: 'm1', has_more: false }),
        );
      }
      if (url === `/api/v1/tickets/${ticketId}/events`) {
        return Promise.resolve(json({ items: [], next_cursor: null }));
      }
      if (url === '/api/v1/agents') {
        return Promise.resolve(json({ items: [] }));
      }
      if (url === `/api/v1/tickets/${ticketId}` && method === 'PATCH') {
        return Promise.resolve(
          json(
            {
              error: {
                code: 'precondition_failed',
                message: 'Ticket changed. Refresh before retrying.',
                fields: {},
              },
            },
            { status: 412 },
          ),
        );
      }
      if (url === `/api/v1/tickets/${ticketId}`) {
        return Promise.resolve(json(staffTicket, { status: 200, headers: { ETag: '"t:1"' } }));
      }
      return Promise.resolve(json({}, { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<TicketDetail ticketId={ticketId} user={staff} base="/agent/tickets" revision={0} />);

    await screen.findByText('We are checking the network with our provider.');
    await user.selectOptions(screen.getByLabelText('Status'), 'RESOLVED');
    await user.click(screen.getByRole('button', { name: /save changes/i }));

    expect(await screen.findByText(/changes were made elsewhere/i)).toBeInTheDocument();
  });
});

describe('TicketDetail for customers', () => {
  it('hides internal tooling and sends a public reply with an idempotency key', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? 'GET';
      if (url === '/api/v1/auth/csrf') {
        return Promise.resolve(json({ csrf_token: 'test-csrf' }));
      }
      if (url.startsWith(`/api/v1/tickets/${ticketId}/messages`) && method === 'GET') {
        return Promise.resolve(
          json({
            items: [customerPublicMessage],
            older_cursor: 'm1',
            newer_cursor: 'm1',
            has_more: false,
          }),
        );
      }
      if (url === `/api/v1/tickets/${ticketId}/messages` && method === 'POST') {
        return Promise.resolve(json({ ...customerPublicMessage, id: 'm2' }, { status: 201 }));
      }
      if (url === `/api/v1/tickets/${ticketId}/public-history`) {
        return Promise.resolve(json({ items: [], next_cursor: null }));
      }
      if (url === `/api/v1/tickets/${ticketId}`) {
        return Promise.resolve(json(customerTicket));
      }
      return Promise.resolve(json({}, { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<TicketDetail ticketId={ticketId} user={customer} base="/support" revision={0} />);

    expect(await screen.findByText('Any update on this?')).toBeInTheDocument();
    expect(screen.queryByText(/internal note/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument();

    await user.type(screen.getByLabelText('Public reply'), 'Still waiting, thanks.');
    await user.click(screen.getByRole('button', { name: /send reply/i }));

    await waitFor(() => {
      const post = fetchMock.mock.calls.find(
        ([url, init]) =>
          String(url) === `/api/v1/tickets/${ticketId}/messages` && init?.method === 'POST',
      );
      expect(post).toBeDefined();
    });
    const post = fetchMock.mock.calls.find(
      ([url, init]) => String(url) === `/api/v1/tickets/${ticketId}/messages` && init?.method === 'POST',
    )!;
    expect(post[1]?.headers).toMatchObject({ 'Idempotency-Key': expect.any(String) });
    expect(JSON.parse(String(post[1]?.body))).toEqual({
      body: 'Still waiting, thanks.',
      visibility: 'PUBLIC',
    });
  });
});

describe('TicketDetail order context', () => {
  const orderedTicket = { ...staffTicket, order_id: 'ORD-DEMO-1001' };

  function mockWithOrder(orderResponse: Response) {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith('/api/v1/tickets/') && url.includes('/messages')) {
        return Promise.resolve(
          json({ items: [], older_cursor: null, newer_cursor: null, has_more: false }),
        );
      }
      if (url === `/api/v1/tickets/${ticketId}/events`) {
        return Promise.resolve(json({ items: [], next_cursor: null }));
      }
      if (url === '/api/v1/agents') {
        return Promise.resolve(json({ items: [{ id: 'agent-1', display_name: 'Agent One' }] }));
      }
      if (url === `/api/v1/mock/orders/ORD-DEMO-1001`) {
        return Promise.resolve(orderResponse);
      }
      if (url === `/api/v1/tickets/${ticketId}`) {
        return Promise.resolve(json(orderedTicket, { status: 200, headers: { ETag: '"t:1"' } }));
      }
      return Promise.resolve(json({}, { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);
  }

  it('renders a read-only summary for a known owned order', async () => {
    mockWithOrder(
      json({
        order_id: 'ORD-DEMO-1001',
        destination: 'Turkey',
        package: '10 GB',
        status: 'completed',
        esim_status: 'installed',
      }),
    );

    render(<TicketDetail ticketId={ticketId} user={staff} base="/agent/tickets" revision={0} />);

    expect(await screen.findByText('Turkey')).toBeInTheDocument();
    expect(screen.getByText('10 GB')).toBeInTheDocument();
    expect(screen.getByText(/completed/i)).toBeInTheDocument();
  });

  it('shows an explicit not-found state without breaking the ticket', async () => {
    mockWithOrder(
      json({ error: { code: 'order_not_found', message: 'Order not found.', fields: {} } }, { status: 404 }),
    );

    render(<TicketDetail ticketId={ticketId} user={staff} base="/agent/tickets" revision={0} />);

    expect(await screen.findByText(/order not found/i)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /ticket status/i })).toBeInTheDocument();
  });

  it('degrades gracefully when order lookup is unavailable', async () => {
    mockWithOrder(
      json(
        {
          error: {
            code: 'order_lookup_unavailable',
            message: 'Order lookup is unavailable.',
            fields: {},
          },
        },
        { status: 503 },
      ),
    );

    render(<TicketDetail ticketId={ticketId} user={staff} base="/agent/tickets" revision={0} />);

    expect(await screen.findByText(/temporarily unavailable/i)).toBeInTheDocument();
  });
});

describe('TicketDetail SLA card', () => {
  function mockWithSla(slaResponse: Response) {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/sla')) {
        return Promise.resolve(slaResponse);
      }
      if (url.startsWith(`/api/v1/tickets/${ticketId}/messages`)) {
        return Promise.resolve(
          json({ items: [], older_cursor: null, newer_cursor: null, has_more: false }),
        );
      }
      if (url === `/api/v1/tickets/${ticketId}/events`) {
        return Promise.resolve(json({ items: [], next_cursor: null }));
      }
      if (url === '/api/v1/agents') {
        return Promise.resolve(json({ items: [{ id: 'agent-1', display_name: 'Agent One' }] }));
      }
      if (url === `/api/v1/tickets/${ticketId}`) {
        return Promise.resolve(json(staffTicket, { status: 200, headers: { ETag: '"t:1"' } }));
      }
      return Promise.resolve(json({}, { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);
  }

  it('shows on-track timers with remaining time and policy metadata', async () => {
    mockWithSla(
      json({
        policy_version: '2026-01-project-defaults',
        priority: 'HIGH',
        cycle_number: 1,
        first_response: {
          state: 'on_track',
          target_seconds: 3600,
          due_at: '2026-09-22T13:00:00Z',
          remaining_seconds: 2400,
          met_at: null,
        },
        resolution: {
          state: 'due_soon',
          target_seconds: 28800,
          due_at: '2026-09-22T20:00:00Z',
          remaining_seconds: 900,
          met_at: null,
        },
      }),
    );

    render(<TicketDetail ticketId={ticketId} user={staff} base="/agent/tickets" revision={0} />);

    expect(await screen.findByText('On track')).toBeInTheDocument();
    expect(screen.getByText(/40m left/)).toBeInTheDocument();
    expect(screen.getByText('Due soon')).toBeInTheDocument();
    expect(screen.getByText(/cycle 1/)).toBeInTheDocument();
  });

  it('shows overdue and met states distinctly', async () => {
    mockWithSla(
      json({
        policy_version: '2026-01-project-defaults',
        priority: 'URGENT',
        cycle_number: 2,
        first_response: {
          state: 'met',
          target_seconds: 900,
          due_at: '2026-09-22T12:15:00Z',
          remaining_seconds: 0,
          met_at: '2026-09-22T12:10:00Z',
        },
        resolution: {
          state: 'overdue',
          target_seconds: 14400,
          due_at: '2026-09-22T16:00:00Z',
          remaining_seconds: 0,
          met_at: null,
        },
      }),
    );

    render(<TicketDetail ticketId={ticketId} user={staff} base="/agent/tickets" revision={0} />);

    expect(await screen.findByText('Met')).toBeInTheDocument();
    expect(screen.getByText('Overdue')).toBeInTheDocument();
    expect(screen.getByText(/past due/)).toBeInTheDocument();
  });

  it('degrades when no active cycle exists', async () => {
    mockWithSla(
      json(
        { error: { code: 'sla_unavailable', message: 'No active SLA cycle.', fields: {} } },
        { status: 404 },
      ),
    );

    render(<TicketDetail ticketId={ticketId} user={staff} base="/agent/tickets" revision={0} />);

    expect(await screen.findByText(/no active sla cycle/i)).toBeInTheDocument();
  });
});

describe('TicketDetail saved replies', () => {
  it('inserts an editable saved reply without sending it', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === '/api/v1/saved-replies' && (init?.method ?? 'GET') === 'GET') {
        return Promise.resolve(
          json({
            items: [
              {
                id: 'sr-1',
                title: 'Connectivity follow-up',
                body: 'Please confirm your current country.',
                category: 'CONNECTIVITY',
                is_active: true,
                author_id: 'agent-1',
                created_at: '2026-09-22T10:00:00Z',
                updated_at: '2026-09-22T10:00:00Z',
              },
            ],
          }),
        );
      }
      if (url.startsWith(`/api/v1/tickets/${ticketId}/messages`)) {
        return Promise.resolve(
          json({ items: [], older_cursor: null, newer_cursor: null, has_more: false }),
        );
      }
      if (url === `/api/v1/tickets/${ticketId}/events`) {
        return Promise.resolve(json({ items: [], next_cursor: null }));
      }
      if (url === '/api/v1/agents') {
        return Promise.resolve(json({ items: [{ id: 'agent-1', display_name: 'Agent One' }] }));
      }
      if (url === `/api/v1/tickets/${ticketId}`) {
        return Promise.resolve(json(staffTicket, { status: 200, headers: { ETag: '"t:1"' } }));
      }
      if (url === `/api/v1/tickets/${ticketId}/sla`) {
        return Promise.resolve(json({}, { status: 404 }));
      }
      return Promise.resolve(json({}, { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<TicketDetail ticketId={ticketId} user={staff} base="/agent/tickets" revision={0} />);

    const picker = await screen.findByRole('combobox', { name: /insert a saved reply/i });
    await user.selectOptions(picker, 'sr-1');

    expect(screen.getByLabelText('Public reply')).toHaveValue(
      'Please confirm your current country.',
    );
    // Inserting text must never send a message.
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) =>
          String(url) === `/api/v1/tickets/${ticketId}/messages` && init?.method === 'POST',
      ),
    ).toBe(false);
  });
});

describe('TicketDetail live reconciliation', () => {
  it('merges an invalidation without duplicating an already-rendered message', async () => {
    const followUp = { ...publicMessage, id: 'm2', body: 'Second canonical message.', position: 2 };
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith(`/api/v1/tickets/${ticketId}/messages`)) {
        const isCatchUp = url.includes('after=');
        return Promise.resolve(
          json(
            isCatchUp
              ? {
                  items: [publicMessage, followUp],
                  older_cursor: 'm1',
                  newer_cursor: 'm2',
                  has_more: false,
                }
              : {
                  items: [publicMessage],
                  older_cursor: null,
                  newer_cursor: 'm1',
                  has_more: false,
                },
          ),
        );
      }
      if (url === `/api/v1/tickets/${ticketId}/events`) {
        return Promise.resolve(json({ items: [], next_cursor: null }));
      }
      if (url === '/api/v1/agents') {
        return Promise.resolve(json({ items: [] }));
      }
      if (url === `/api/v1/tickets/${ticketId}`) {
        return Promise.resolve(json(staffTicket, { status: 200, headers: { ETag: '"t:1"' } }));
      }
      return Promise.resolve(json({}, { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);

    const view = (
      <TicketDetail ticketId={ticketId} user={staff} base="/agent/tickets" revision={0} />
    );
    const { rerender } = render(view);
    expect(await screen.findByText(publicMessage.body)).toBeInTheDocument();

    // An SSE invalidation arrives; the catch-up response repeats m1 and adds m2.
    rerender(<TicketDetail ticketId={ticketId} user={staff} base="/agent/tickets" revision={1} />);

    expect(screen.getByRole('heading', { name: staffTicket.subject })).toBeInTheDocument();
    expect(screen.queryByText('Loading ticket…')).not.toBeInTheDocument();
    expect(await screen.findByText(followUp.body)).toBeInTheDocument();
    expect(screen.getAllByText(publicMessage.body)).toHaveLength(1);
    expect(screen.getAllByText(followUp.body)).toHaveLength(1);
  });
});
