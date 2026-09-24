import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import {
  ApiError,
  createTicket,
  listAgents,
  listTickets,
  lookupCustomers,
  newIdempotencyKey,
  type AgentSummary,
  type AgentTicket,
  type CustomerSummary,
  type Identity,
  type Ticket,
  type TicketListFilters,
} from './api';
import { Link, navigate } from './navigation';
import { Badge, CATEGORIES, ErrorNotice, Field, PRIORITIES, STATUSES, Timestamp, label } from './shared';

function isAgentTicket(ticket: Ticket): ticket is AgentTicket {
  return 'customer_email_snapshot' in ticket;
}

function searchOf(location: string): string {
  const index = location.indexOf('?');
  return index === -1 ? '' : location.slice(index + 1);
}

const STAFF_VIEWS = [
  { key: 'all', label: 'All' },
  { key: 'unassigned', label: 'Unassigned' },
  { key: 'mine', label: 'Mine' },
] as const;

type TicketListProps = {
  user: Identity;
  base: string;
  detailBase: string;
  location: string;
  revision: number;
};

function PriorityIndicator({ value }: { value: string }) {
  return <span className={`priority-cell priority-${value.toLowerCase()}`}><span className="priority-dot" aria-hidden="true" />{label(value)}</span>;
}

export function TicketList({ user, base, detailBase, location, revision }: TicketListProps) {
  const staff = user.role !== 'CUSTOMER';
  const search = searchOf(location);
  const params = new URLSearchParams(search);
  const view = staff ? (params.get('view') ?? 'all') : 'all';
  const status = params.get('status') ?? '';
  const priority = params.get('priority') ?? '';
  const category = params.get('category') ?? '';
  const query = params.get('query') ?? '';

  const [reload, setReload] = useState(0);
  const [draftSearch, setDraftSearch] = useState<string | null>(null);
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [loadingMore, setLoadingMore] = useState(false);
  const [result, setResult] = useState<{
    filtersKey: string;
    requestKey: string;
    items: Ticket[];
    next: string | null;
    error?: unknown;
  }>({ filtersKey: '', requestKey: '', items: [], next: null });

  const searchText = draftSearch ?? query;

  const filters = useMemo<TicketListFilters>(
    () => ({
      status,
      priority,
      category,
      query,
      limit: 25,
      assigned_agent_id: view === 'mine' ? user.id : undefined,
      unassigned: view === 'unassigned' ? true : undefined,
    }),
    [status, priority, category, query, view, user.id],
  );
  const filtersKey = JSON.stringify(filters);
  const requestKey = `${filtersKey}|r${revision}|${reload}`;

  const applyFilters = useCallback(
    (updates: Record<string, string>) => {
      const nextParams = new URLSearchParams(search);
      for (const [key, value] of Object.entries(updates)) {
        if (value) nextParams.set(key, value);
        else nextParams.delete(key);
      }
      nextParams.delete('cursor');
      const encoded = nextParams.toString();
      navigate(`${base}${encoded ? `?${encoded}` : ''}`, true);
    },
    [base, search],
  );

  useEffect(() => {
    if (draftSearch === null || draftSearch === query) return;
    const timer = window.setTimeout(() => {
      applyFilters({ query: draftSearch });
      setDraftSearch(null);
    }, 300);
    return () => window.clearTimeout(timer);
  }, [draftSearch, query, applyFilters]);

  useEffect(() => {
    if (!staff) return;
    const controller = new AbortController();
    void listAgents(controller.signal)
      .then((loaded) => {
        if (!controller.signal.aborted) setAgents(loaded);
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, [staff]);

  useEffect(() => {
    const controller = new AbortController();
    void listTickets(filters, controller.signal)
      .then((page) => {
        if (controller.signal.aborted) return;
        setResult({ filtersKey, requestKey, items: page.items, next: page.next_cursor });
      })
      .catch((error) => {
        if (controller.signal.aborted) return;
        setResult((current) =>
          current.filtersKey === filtersKey && current.items.length > 0
            ? { ...current, requestKey, error }
            : { filtersKey, requestKey, items: [], next: null, error },
        );
      });
    return () => controller.abort();
  }, [filters, filtersKey, requestKey]);

  const loading = result.filtersKey !== filtersKey;
  const items = result.filtersKey === filtersKey ? result.items : [];
  const next = result.filtersKey === filtersKey && result.requestKey === requestKey ? result.next : null;
  const error = result.filtersKey === filtersKey ? result.error : undefined;
  const hasFilters = Boolean(status || priority || category || query || view !== 'all');

  const loadMore = useCallback(() => {
    if (!next || loadingMore) return;
    setLoadingMore(true);
    void listTickets({ ...filters, cursor: next })
      .then((page) => {
        setResult((current) =>
          current.filtersKey === filtersKey && current.requestKey === requestKey
            ? {
                ...current,
                items: [
                  ...current.items,
                  ...page.items.filter(
                    (ticket) => !current.items.some((existing) => existing.id === ticket.id),
                  ),
                ],
                next: page.next_cursor,
              }
            : current,
        );
      })
      .catch((failure) => setResult((current) => ({ ...current, error: failure })))
      .finally(() => setLoadingMore(false));
  }, [filters, filtersKey, next, requestKey, loadingMore]);

  const agentName = (id: string | null): string => {
    if (!id) return 'Unassigned';
    return agents.find((agent) => agent.id === id)?.display_name ?? 'Assigned';
  };

  return (
    <section className="page ticket-page" aria-labelledby="queue-title">
      <div className="page-head">
        <h1 id="queue-title">{staff ? 'Support queue' : 'My tickets'}</h1>
        <Link className="button ticket-new" href={`${base}/new`}>
          <span aria-hidden="true">＋</span> New ticket
        </Link>
      </div>

      {staff && (
        <nav className="view-tabs" aria-label="Queue views">
          {STAFF_VIEWS.map((option) => (
            <Link
              key={option.key}
              href={option.key === 'all' ? base : `${base}?view=${option.key}`}
              className={view === option.key ? 'view-tab view-tab-active' : 'view-tab'}
              aria-current={view === option.key ? 'page' : undefined}
            >
              {option.label}
            </Link>
          ))}
        </nav>
      )}

      <form
        className="filter-bar"
        role="search"
        onSubmit={(event) => {
          event.preventDefault();
          applyFilters({ query: searchText });
        }}
      >
        <label className="filter-field">
          <span>Search</span>
          <input
            type="search"
            value={searchText}
            onChange={(event) => setDraftSearch(event.target.value)}
            placeholder={staff ? 'Search by number, subject, customer…' : 'Search by number, subject…'}
            maxLength={200}
          />
        </label>
        <label className="filter-field">
          <span>Status</span>
          <select value={status} onChange={(event) => applyFilters({ status: event.target.value })}>
            <option value="">All statuses</option>
            {STATUSES.map((value) => (
              <option key={value} value={value}>
                {label(value)}
              </option>
            ))}
          </select>
        </label>
        <label className="filter-field">
          <span>Priority</span>
          <select value={priority} onChange={(event) => applyFilters({ priority: event.target.value })}>
            <option value="">All priorities</option>
            {PRIORITIES.map((value) => (
              <option key={value} value={value}>
                {label(value)}
              </option>
            ))}
          </select>
        </label>
        <label className="filter-field">
          <span>Category</span>
          <select value={category} onChange={(event) => applyFilters({ category: event.target.value })}>
            <option value="">All categories</option>
            {CATEGORIES.map((value) => (
              <option key={value} value={value}>
                {label(value)}
              </option>
            ))}
          </select>
        </label>
        {hasFilters && (
          <button
            type="button"
            className="button secondary filter-clear"
            onClick={() => {
              setDraftSearch(null);
              navigate(base, true);
            }}
          >
            Clear
          </button>
        )}
      </form>

      {error !== undefined && <ErrorNotice error={error} retry={() => setReload((value) => value + 1)} />}

      {loading ? (
        <p className="muted" role="status">
          Loading tickets…
        </p>
      ) : items.length === 0 && error === undefined ? (
        <section className="empty-panel">
          <h2>{hasFilters ? 'No matching tickets' : 'No tickets yet'}</h2>
          <p>
            {hasFilters
              ? 'Try clearing filters or searching a different term.'
              : staff
                ? 'Create a ticket on behalf of a customer to get started.'
                : 'Open a ticket and our team will reply here.'}
          </p>
          {hasFilters ? (
            <button
              type="button"
              className="button secondary"
              onClick={() => {
                setDraftSearch(null);
                navigate(base, true);
              }}
            >
              Clear filters
            </button>
          ) : (
            <Link className="button" href={`${base}/new`}>
              New ticket
            </Link>
          )}
        </section>
      ) : (
        <>
          <div
            className={staff ? 'ticket-list ticket-list-staff' : 'ticket-list ticket-list-customer'}
            role="table"
            aria-label="Tickets"
          >
            <div className="ticket-list-head" role="row">
              <span role="columnheader">#</span>
              <span role="columnheader">Subject</span>
              {staff && <span role="columnheader">Customer</span>}
              <span role="columnheader">Status</span>
              <span role="columnheader">Priority</span>
              <span role="columnheader">Category</span>
              {staff && <span role="columnheader">Assignee</span>}
              <span role="columnheader">Updated</span>
            </div>
            <ul className="ticket-rows">
              {items.map((ticket) => {
                const detail = isAgentTicket(ticket);
                const assignee = detail ? agentName(ticket.assigned_agent_id) : '';
                return (
                  <li key={ticket.id} role="row">
                    <Link className="ticket-row" href={`${detailBase}/${ticket.id}`}>
                      <span className="ticket-number" data-label="Number">
                        {ticket.ticket_number}
                      </span>
                      <span className="ticket-subject" data-label="Subject">
                        {(ticket.unread_count ?? 0) > 0 && (
                          <>
                            <span className="unread-dot" aria-hidden="true" />
                            <span className="visually-hidden">{ticket.unread_count} unread</span>
                          </>
                        )}
                        {ticket.subject}
                      </span>
                      {staff && (
                        <span className="ticket-customer muted" data-label="Customer">
                          {detail ? ticket.customer_email_snapshot : ''}
                        </span>
                      )}
                      <span data-label="Status">
                        <Badge value={ticket.status} />
                      </span>
                      <span data-label="Priority">
                        <PriorityIndicator value={ticket.priority} />
                      </span>
                      <span className="muted" data-label="Category">
                        {label(ticket.category)}
                      </span>
                      {staff && (
                        <span
                          className={
                            detail && !ticket.assigned_agent_id
                              ? 'ticket-assignee ticket-assignee-unassigned'
                              : 'ticket-assignee'
                          }
                          data-label="Assignee"
                        >
                          {assignee}
                        </span>
                      )}
                      <span className="muted" data-label="Updated">
                        <Timestamp value={detail ? ticket.updated_at : ticket.public_updated_at} />
                      </span>
                    </Link>
                  </li>
                );
              })}
            </ul>
          </div>
          {next && (
            <div className="load-more">
              <button type="button" className="button secondary" onClick={loadMore} disabled={loadingMore}>
                {loadingMore ? 'Loading…' : 'Load more'}
              </button>
            </div>
          )}
        </>
      )}
    </section>
  );
}

type TicketCreateProps = {
  user: Identity;
  base: string;
  detailBase: string;
};

export function TicketCreate({ user, base, detailBase }: TicketCreateProps) {
  const staff = user.role !== 'CUSTOMER';
  const [category, setCategory] = useState('');
  const [subject, setSubject] = useState('');
  const [description, setDescription] = useState('');
  const [priority, setPriority] = useState('MEDIUM');
  const [orderId, setOrderId] = useState('');
  const [customerQuery, setCustomerQuery] = useState('');
  const [customerResults, setCustomerResults] = useState<CustomerSummary[]>([]);
  const [customerSearching, setCustomerSearching] = useState(false);
  const [customer, setCustomer] = useState<CustomerSummary | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>();
  const keyRef = useRef(newIdempotencyKey());

  const showCustomerResults = staff && !customer && customerQuery.trim().length >= 2;

  useEffect(() => {
    if (!staff || customer) return;
    const term = customerQuery.trim();
    if (term.length < 2) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      setCustomerSearching(true);
      void lookupCustomers(term, controller.signal)
        .then((results) => {
          if (controller.signal.aborted) return;
          setCustomerResults(results);
          setCustomerSearching(false);
        })
        .catch(() => {
          if (controller.signal.aborted) return;
          setCustomerResults([]);
          setCustomerSearching(false);
        });
    }, 300);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [staff, customer, customerQuery]);

  const resetKey = () => {
    keyRef.current = newIdempotencyKey();
  };

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (pending) return;
    const trimmedSubject = subject.trim();
    const trimmedDescription = description.trim();
    if (!category || !trimmedSubject || !trimmedDescription) {
      setError(
        new ApiError(422, 'validation_error', 'Complete the required fields.', {
          'body.category': category ? '' : 'Select a category.',
          'body.subject': trimmedSubject ? '' : 'Enter a subject.',
          'body.description': trimmedDescription ? '' : 'Enter a description.',
        }),
      );
      return;
    }
    if (staff && !customer) {
      setError(
        new ApiError(422, 'validation_error', 'Select the customer this ticket is for.', {
          'body.customer_email': 'Choose an active customer.',
        }),
      );
      return;
    }
    setPending(true);
    setError(undefined);
    try {
      const created = await createTicket(
        {
          customer_email: staff ? (customer?.email ?? '') : user.email,
          customer_id: staff ? (customer?.id ?? null) : undefined,
          category,
          subject: trimmedSubject,
          description: trimmedDescription,
          priority,
          order_id: orderId.trim() || null,
        },
        keyRef.current,
      );
      navigate(`${detailBase}/${created.id}`);
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 409) {
        resetKey();
      }
      setError(failure);
      setPending(false);
    }
  }

  return (
    <section className="page page-narrow create-page" aria-labelledby="create-title">
      <div className="page-head">
        <h1 id="create-title">Open a ticket</h1>
      </div>

      <form className="card form-card" onSubmit={submit} noValidate>
        {staff && (
          <div className="field customer-picker">
            <span>Customer</span>
            {customer ? (
              <div className="customer-selected">
                <span>
                  <strong>{customer.display_name}</strong>{' '}
                  <span className="muted">{customer.email}</span>
                </span>
                <button
                  type="button"
                  className="text-button"
                  onClick={() => {
                    setCustomer(null);
                    setCustomerQuery('');
                    setCustomerResults([]);
                    resetKey();
                  }}
                >
                  Change
                </button>
              </div>
            ) : (
              <>
                <input
                  type="search"
                  value={customerQuery}
                  onChange={(event) => setCustomerQuery(event.target.value)}
                  placeholder="Search customer name or email"
                  maxLength={254}
                  aria-describedby="customer-hint"
                />
                <small id="customer-hint">
                  Type at least two characters, then select the matching account.
                </small>
                {customerSearching && <small role="status">Searching…</small>}
                {customerResults.length > 0 && (
                  <ul className="customer-options" role="listbox" aria-label="Customer results">
                    {customerResults.map((option) => (
                      <li key={option.id}>
                        <button
                          type="button"
                          role="option"
                          aria-selected={false}
                          onClick={() => {
                            setCustomer(option);
                            setCustomerQuery(option.email);
                            setCustomerResults([]);
                            resetKey();
                          }}
                        >
                          <strong>{option.display_name}</strong>{' '}
                          <span className="muted">{option.email}</span>
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
                {showCustomerResults && !customerSearching && customerResults.length === 0 && (
                  <small className="muted">No active customer matches that search.</small>
                )}
              </>
            )}
          </div>
        )}

        <Field label="Category">
          <select
            value={category}
            onChange={(event) => {
              setCategory(event.target.value);
              resetKey();
            }}
            required
          >
            <option value="">Select a category</option>
            {CATEGORIES.map((value) => (
              <option key={value} value={value}>
                {label(value)}
              </option>
            ))}
          </select>
        </Field>

        <Field label="Subject">
          <input
            type="text"
            value={subject}
            onChange={(event) => {
              setSubject(event.target.value);
              resetKey();
            }}
            maxLength={200}
            placeholder="Brief summary of the issue"
            required
          />
        </Field>

        <Field label="Description" hint="Describe what happened and any steps you already tried.">
          <textarea
            value={description}
            onChange={(event) => {
              setDescription(event.target.value);
              resetKey();
            }}
            maxLength={10000}
            rows={6}
            placeholder="What happened? Include any steps you've already tried."
            required
          />
        </Field>

        <div className="form-grid">
          <Field label="Priority">
            <select
              value={priority}
              onChange={(event) => {
                setPriority(event.target.value);
                resetKey();
              }}
            >
              {PRIORITIES.map((value) => (
                <option key={value} value={value}>
                  {label(value)}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Order reference (optional)">
            <input
              type="text"
              value={orderId}
              onChange={(event) => {
                setOrderId(event.target.value);
                resetKey();
              }}
              maxLength={64}
              placeholder="ORD-10293"
            />
          </Field>
        </div>

        {error !== undefined && <ErrorNotice error={error} />}

        <div className="form-actions">
          <div className="form-actions-left">
            <button type="submit" disabled={pending}>
              {pending ? 'Creating…' : 'Create ticket'}
            </button>
            <Link className="button secondary" href={base}>
              Cancel
            </Link>
          </div>
          <p className="form-security-note">
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.7"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <path d="M12 3 4 6v5c0 5 3.4 8.7 8 10 4.6-1.3 8-5 8-10V6l-8-3Z" />
              <path d="M9.5 12 11 13.5l3.5-4" />
            </svg>
            Never include activation QR codes, passwords, or payment details in a support request.
          </p>
        </div>
      </form>
    </section>
  );
}
