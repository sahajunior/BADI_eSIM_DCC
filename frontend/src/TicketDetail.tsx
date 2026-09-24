import { useEffect, useLayoutEffect, useRef, useState, type FormEvent } from 'react';
import {
  ApiError,
  createMessage,
  getOrder,
  getTicket,
  getTicketSla,
  listAgents,
  listMessages,
  listPublicHistory,
  listSavedReplies,
  listTicketEvents,
  markTicketSeen,
  newIdempotencyKey,
  patchTicket,
  uploadAttachment,
  type AgentSummary,
  type AgentTicket,
  type Identity,
  type OrderSummary,
  type PublicHistoryItem,
  type SavedReplyDTO,
  type SlaTimerDTO,
  type Ticket,
  type TicketEventDTO,
  type TicketMessage,
  type TicketPatchInput,
  type TicketSlaDTO,
} from './api';
import { Link } from './navigation';
import { Badge, ErrorNotice, Field, PRIORITIES, STATUSES, Timestamp, label } from './shared';

function isAgentTicket(ticket: Ticket): ticket is AgentTicket {
  return 'customer_email_snapshot' in ticket;
}

function isInternal(message: TicketMessage): boolean {
  return 'visibility' in message && message.visibility === 'INTERNAL';
}

type ComposerMode = 'PUBLIC' | 'INTERNAL';

type MetadataFormProps = {
  ticket: AgentTicket;
  etag: string;
  agents: AgentSummary[];
  onSaved: () => void;
};

function MetadataForm({ ticket, etag, agents, onSaved }: MetadataFormProps) {
  const [statusValue, setStatusValue] = useState(ticket.status);
  const [priorityValue, setPriorityValue] = useState(ticket.priority);
  const [assigneeValue, setAssigneeValue] = useState(ticket.assigned_agent_id ?? '');
  const [reason, setReason] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>();
  const [stale, setStale] = useState(false);

  const assigneeOptions =
    assigneeValue && !agents.some((agent) => agent.id === assigneeValue)
      ? [{ id: assigneeValue, display_name: 'Current assignee' }, ...agents]
      : agents;

  async function save(event: FormEvent) {
    event.preventDefault();
    if (pending) return;
    const patch: TicketPatchInput = {};
    if (statusValue !== ticket.status) patch.status = statusValue;
    if (priorityValue !== ticket.priority) patch.priority = priorityValue;
    if (assigneeValue !== (ticket.assigned_agent_id ?? '')) {
      patch.assigned_agent_id = assigneeValue || null;
    }
    if (reason.trim()) patch.reason = reason.trim();
    if (Object.keys(patch).length === 0) {
      setError(new ApiError(422, 'empty_patch', 'Choose a change before saving.'));
      return;
    }
    setPending(true);
    setError(undefined);
    setStale(false);
    try {
      await patchTicket(ticket.id, patch, etag);
      setReason('');
      setPending(false);
      onSaved();
    } catch (failure) {
      if (failure instanceof ApiError && (failure.status === 412 || failure.status === 428)) {
        setStale(true);
      } else {
        setError(failure);
      }
      setPending(false);
    }
  }

  return (
    <form className="card metadata-card" onSubmit={save}>
      <h2>Ticket status</h2>
      {stale && (
        <div className="stale-notice" role="alert">
          <strong>Changes were made elsewhere.</strong>
          <p>Review the latest ticket before saving.</p>
          <button
            type="button"
            className="button secondary"
            onClick={() => {
              setStale(false);
              onSaved();
            }}
          >
            Reload latest
          </button>
        </div>
      )}
      <Field label="Status">
        <select value={statusValue} onChange={(event) => setStatusValue(event.target.value)}>
          {STATUSES.map((value) => (
            <option key={value} value={value}>
              {label(value)}
            </option>
          ))}
        </select>
      </Field>
      <Field label="Priority">
        <select value={priorityValue} onChange={(event) => setPriorityValue(event.target.value)}>
          {PRIORITIES.map((value) => (
            <option key={value} value={value}>
              {label(value)}
            </option>
          ))}
        </select>
      </Field>
      <Field label="Assignee">
        <select value={assigneeValue} onChange={(event) => setAssigneeValue(event.target.value)}>
          <option value="">Unassigned</option>
          {assigneeOptions.map((agent) => (
            <option key={agent.id} value={agent.id}>
              {agent.display_name}
            </option>
          ))}
        </select>
      </Field>
      <Field label="Note (optional)" hint="Shown to staff; required when reopening a closed ticket.">
        <input
          type="text"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          maxLength={1000}
        />
      </Field>
      {error !== undefined && <ErrorNotice error={error} />}
      <button type="submit" disabled={pending}>
        {pending ? 'Saving…' : 'Save changes'}
      </button>
    </form>
  );
}

type SlaCardProps = {
  ticketId: string;
  refreshKey: string;
};

function formatRemaining(seconds: number | null): string {
  if (seconds === null) return '';
  if (seconds <= 0) return 'now';
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.round((seconds % 3600) / 60);
  return hours > 0 ? `${hours}h ${minutes}m` : `${minutes}m`;
}

const SLA_STATES: Record<string, string> = {
  met: 'Met',
  on_track: 'On track',
  due_soon: 'Due soon',
  overdue: 'Overdue',
  paused: 'Paused',
};

function SlaTimer({ label, timer }: { label: string; timer: SlaTimerDTO }) {
  const text = SLA_STATES[timer.state] ?? label;
  return (
    <div>
      <dt>{label}</dt>
      <dd>
        <span className={`sla-state sla-${timer.state}`}>{text}</span>
        {(timer.state === 'on_track' || timer.state === 'due_soon') && (
          <span className="muted"> · {formatRemaining(timer.remaining_seconds)} left</span>
        )}
        {timer.state === 'overdue' && timer.remaining_seconds === 0 && (
          <span className="muted"> · past due</span>
        )}
      </dd>
    </div>
  );
}

function SlaCard({ ticketId, refreshKey }: SlaCardProps) {
  const [state, setState] = useState<{
    status: 'loading' | 'ready' | 'missing' | 'error';
    sla?: TicketSlaDTO;
  }>({ status: 'loading' });

  useEffect(() => {
    const controller = new AbortController();
    void getTicketSla(ticketId, controller.signal)
      .then((sla) => {
        if (!controller.signal.aborted) setState({ status: 'ready', sla });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setState({
          status: error instanceof ApiError && error.status === 404 ? 'missing' : 'error',
        });
      });
    return () => controller.abort();
  }, [ticketId, refreshKey]);

  return (
    <section className="card sla-card" aria-labelledby="sla-title">
      <h2 id="sla-title">Service level</h2>
      {state.status === 'loading' && (
        <p className="muted" role="status">
          Loading SLA…
        </p>
      )}
      {state.status === 'missing' && (
        <p className="muted">No active SLA cycle (the ticket is resolved or closed).</p>
      )}
      {state.status === 'error' && (
        <p className="muted">SLA information is temporarily unavailable.</p>
      )}
      {state.status === 'ready' && state.sla && (
        <>
          <dl className="sla-grid">
            <SlaTimer label="First response" timer={state.sla.first_response} />
            <SlaTimer label="Resolution" timer={state.sla.resolution} />
          </dl>
          <p className="muted sla-footnote">
            Policy {state.sla.policy_version} · cycle {state.sla.cycle_number}
          </p>
        </>
      )}
    </section>
  );
}

type OrderContextState = {
  status: 'loading' | 'ready' | 'missing' | 'unavailable';
  order?: OrderSummary;
};

function OrderContext({ orderId }: { orderId: string }) {
  const [state, setState] = useState<OrderContextState>({ status: 'loading' });

  useEffect(() => {
    const controller = new AbortController();
    void getOrder(orderId, controller.signal)
      .then((order) => {
        if (!controller.signal.aborted) setState({ status: 'ready', order });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setState({ status: error instanceof ApiError && error.status === 404 ? 'missing' : 'unavailable' });
      });
    return () => controller.abort();
  }, [orderId]);

  return (
    <section className="card order-card" aria-labelledby="order-title">
      <h2 id="order-title">Order context</h2>
      <p className="muted order-id">{orderId}</p>
      {state.status === 'loading' && (
        <p className="muted" role="status">
          Loading order…
        </p>
      )}
      {state.status === 'missing' && (
        <p className="muted">Order not found. This reference is shown as plain text only.</p>
      )}
      {state.status === 'unavailable' && (
        <p className="muted">Order information is temporarily unavailable.</p>
      )}
      {state.status === 'ready' && state.order && (
        <dl className="order-grid">
          <div>
            <dt>Destination</dt>
            <dd>{state.order.destination}</dd>
          </div>
          <div>
            <dt>Package</dt>
            <dd>{state.order.package}</dd>
          </div>
          <div>
            <dt>Order status</dt>
            <dd>{label(state.order.status)}</dd>
          </div>
          <div>
            <dt>eSIM</dt>
            <dd>{label(state.order.esim_status)}</dd>
          </div>
        </dl>
      )}
    </section>
  );
}

type TicketDetailProps = {
  ticketId: string;
  user: Identity;
  base: string;
  revision: number;
};

export function TicketDetail({ ticketId, user, base, revision }: TicketDetailProps) {
  const staff = user.role !== 'CUSTOMER';

  const [ticketReload, setTicketReload] = useState(0);
  const [messagesReload, setMessagesReload] = useState(0);
  const [events, setEvents] = useState<TicketEventDTO[]>([]);
  const [history, setHistory] = useState<PublicHistoryItem[]>([]);
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [savedReplies, setSavedReplies] = useState<SavedReplyDTO[]>([]);
  const [olderError, setOlderError] = useState<unknown>();

  const [mode, setMode] = useState<ComposerMode>('PUBLIC');
  const [drafts, setDrafts] = useState<Record<ComposerMode, string>>({ PUBLIC: '', INTERNAL: '' });
  const [sendKey, setSendKey] = useState(newIdempotencyKey());
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<unknown>();
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [showNew, setShowNew] = useState(false);

  const [messages, setMessages] = useState<TicketMessage[]>([]);
  const [messagesMeta, setMessagesMeta] = useState<{
    key: string;
    older: string | null;
    hasMore: boolean;
    error?: unknown;
  }>({ key: '', older: null, hasMore: false });

  const newestId = useRef<string | null>(null);
  const previousRevision = useRef(revision);
  const conversationRef = useRef<HTMLDivElement>(null);
  const atBottomRef = useRef(true);
  const prepended = useRef(false);
  const pendingScroll = useRef<{ height: number; top: number } | null>(null);

  const ticketRequestKey = `${ticketId}:${ticketReload}:${revision}`;
  const [ticketState, setTicketState] = useState<{
    resourceKey: string;
    requestKey: string;
    ticket?: Ticket;
    etag: string | null;
    error?: unknown;
  }>({ resourceKey: '', requestKey: '', etag: null });

  const messagesKey = `${ticketId}:${messagesReload}`;

  useEffect(() => {
    const controller = new AbortController();
    void getTicket(ticketId, controller.signal)
      .then(({ ticket: loaded, etag: loadedEtag }) => {
        if (controller.signal.aborted) return;
        setTicketState({ resourceKey: ticketId, requestKey: ticketRequestKey, ticket: loaded, etag: loadedEtag });
      })
      .catch((error) => {
        if (controller.signal.aborted) return;
        setTicketState((current) =>
          current.resourceKey === ticketId && current.ticket
            ? { ...current, requestKey: ticketRequestKey, error }
            : { resourceKey: ticketId, requestKey: ticketRequestKey, etag: null, error },
        );
      });
    return () => controller.abort();
  }, [ticketId, ticketRequestKey]);

  useEffect(() => {
    const controller = new AbortController();
    void listMessages(ticketId, { limit: 50 }, controller.signal)
      .then((page) => {
        if (controller.signal.aborted) return;
        setMessages(page.items);
        newestId.current = page.newer_cursor;
        setMessagesMeta({ key: messagesKey, older: page.older_cursor, hasMore: page.has_more });
      })
      .catch((error) => {
        if (controller.signal.aborted) return;
        setMessagesMeta({ key: messagesKey, older: null, hasMore: false, error });
      });
    return () => controller.abort();
  }, [ticketId, messagesKey]);

  useEffect(() => {
    if (previousRevision.current === revision) return;
    previousRevision.current = revision;
    const controller = new AbortController();
    void listMessages(
      ticketId,
      { limit: 50, after: newestId.current ?? undefined },
      controller.signal,
    )
      .then((page) => {
        if (controller.signal.aborted) return;
        if (page.items.length > 0 && !atBottomRef.current) setShowNew(true);
        setMessages((current) => {
          const seen = new Set(current.map((message) => message.id));
          const additions = page.items.filter((message) => !seen.has(message.id));
          return additions.length ? [...current, ...additions] : current;
        });
        if (page.newer_cursor) newestId.current = page.newer_cursor;
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, [revision, ticketId]);

  useEffect(() => {
    const controller = new AbortController();
    if (staff) {
      void listTicketEvents(ticketId, controller.signal)
        .then((page) => {
          if (!controller.signal.aborted) setEvents(page.items);
        })
        .catch(() => undefined);
    } else {
      void listPublicHistory(ticketId, controller.signal)
        .then((page) => {
          if (!controller.signal.aborted) setHistory(page.items);
        })
        .catch(() => undefined);
    }
    return () => controller.abort();
  }, [staff, ticketId, revision]);

  useEffect(() => {
    if (!staff) return;
    const controller = new AbortController();
    void listAgents(controller.signal)
      .then((loaded) => {
        if (!controller.signal.aborted) setAgents(loaded);
      })
      .catch(() => undefined);
    void listSavedReplies(controller.signal)
      .then((loaded) => {
        if (!controller.signal.aborted) setSavedReplies(loaded);
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, [staff]);

  useLayoutEffect(() => {
    const node = conversationRef.current;
    if (node && pendingScroll.current) {
      const delta = node.scrollHeight - pendingScroll.current.height;
      node.scrollTop = pendingScroll.current.top + delta;
      pendingScroll.current = null;
      prepended.current = true;
    }
  }, [messages]);

  useEffect(() => {
    const node = conversationRef.current;
    if (!node) return;
    if (prepended.current) {
      prepended.current = false;
      return;
    }
    if (atBottomRef.current) node.scrollTop = node.scrollHeight;
  }, [messages]);

  function onScroll() {
    const node = conversationRef.current;
    if (!node) return;
    const nearBottom = node.scrollHeight - node.scrollTop - node.clientHeight < 80;
    atBottomRef.current = nearBottom;
    if (nearBottom) setShowNew(false);
  }

  async function loadOlder() {
    if (!hasMore || !olderCursor) return;
    const node = conversationRef.current;
    if (node) pendingScroll.current = { height: node.scrollHeight, top: node.scrollTop };
    setOlderError(undefined);
    try {
      const page = await listMessages(ticketId, { limit: 50, before: olderCursor });
      setMessages((current) => {
        const seen = new Set(current.map((message) => message.id));
        return [...page.items.filter((message) => !seen.has(message.id)), ...current];
      });
      setMessagesMeta((current) =>
        current.key === messagesKey
          ? { ...current, older: page.older_cursor, hasMore: page.has_more }
          : current,
      );
    } catch (failure) {
      setOlderError(failure);
    }
  }

  async function sendReply() {
    const body = drafts[effectiveMode].trim();
    if ((!body && pendingFiles.length === 0) || sending) return;
    setSending(true);
    setSendError(undefined);
    try {
      const attachmentIds: string[] = [];
      for (const file of pendingFiles) {
        const uploaded = await uploadAttachment(ticketId, file);
        attachmentIds.push(uploaded.id);
      }
      const created = await createMessage(
        ticketId,
        {
          body: body || '(attachment)',
          visibility: effectiveMode,
          ...(attachmentIds.length ? { attachment_ids: attachmentIds } : {}),
        },
        sendKey,
      );
      setMessages((current) =>
        current.some((message) => message.id === created.id) ? current : [...current, created],
      );
      newestId.current = created.id;
      setDrafts((current) => ({ ...current, [effectiveMode]: '' }));
      setPendingFiles([]);
      setSendKey(newIdempotencyKey());
      setShowNew(false);
      atBottomRef.current = true;
      setSending(false);
      setTicketReload((value) => value + 1);
    } catch (failure) {
      setSendError(failure);
      setSending(false);
    }
  }

  const ticket = ticketState.resourceKey === ticketId ? ticketState.ticket : undefined;
  const ticketLoading = !ticket && ticketState.requestKey !== ticketRequestKey;
  const etag = ticketState.resourceKey === ticketId ? ticketState.etag : null;
  const ticketError = ticketState.resourceKey === ticketId ? ticketState.error : undefined;

  const messagesLoading = messagesMeta.key !== messagesKey;
  const messagesError = messagesMeta.key === messagesKey ? messagesMeta.error : undefined;
  const olderCursor = messagesMeta.key === messagesKey ? messagesMeta.older : null;
  const hasMore = messagesMeta.key === messagesKey ? messagesMeta.hasMore : false;
  const visibleMessages = messagesMeta.key === messagesKey ? messages : [];

  const closed = ticket?.status === 'CLOSED';
  const canCompose = staff || !closed;
  const effectiveMode: ComposerMode = closed && staff ? 'INTERNAL' : mode;

  const seenTicketId = ticket?.id;
  useEffect(() => {
    if (!seenTicketId) return;
    // Private unread marker for this viewer; never a public read receipt.
    void markTicketSeen(seenTicketId).catch(() => undefined);
  }, [seenTicketId, revision]);

  return (
    <section className="page" aria-labelledby="ticket-title">
      <div className="detail-top">
        <Link className="text-link" href={base}>
          ← {staff ? 'Support queue' : 'My tickets'}
        </Link>
      </div>

      {ticketLoading && !ticket && (
        <p className="muted" role="status">
          Loading ticket…
        </p>
      )}
      {ticketError !== undefined && !ticket && (
        <ErrorNotice error={ticketError} retry={() => setTicketReload((value) => value + 1)} />
      )}

      {ticket && (
        <>
          <header className="ticket-header">
            <div>
              <p className="eyebrow">{ticket.ticket_number}</p>
              <h1 id="ticket-title">{ticket.subject}</h1>
              <p className="ticket-meta muted">
                <Badge value={ticket.status} /> <Badge value={ticket.priority} />
                <span> · {label(ticket.category)}</span>
                <span>
                  {' '}
                  · opened <Timestamp value={ticket.created_at} />
                </span>
                {isAgentTicket(ticket) && <span> · {ticket.customer_email_snapshot}</span>}
                {ticket.order_id && <span> · order {ticket.order_id}</span>}
              </p>
            </div>
          </header>

          <div className="detail-layout">
            <div className="conversation-panel">
              <div
                className="conversation"
                ref={conversationRef}
                onScroll={onScroll}
                aria-label="Conversation"
              >
                <article className="entry entry-initial">
                  <header className="entry-head">
                    <strong>Customer</strong>
                    <span className="muted">
                      Original request · <Timestamp value={ticket.created_at} />
                    </span>
                  </header>
                  <p className="entry-body">{ticket.description}</p>
                </article>

                {messagesLoading && (
                  <p className="muted" role="status">
                    Loading conversation…
                  </p>
                )}
                {messagesError !== undefined && (
                  <ErrorNotice error={messagesError} retry={() => setMessagesReload((value) => value + 1)} />
                )}

                {hasMore && (
                  <div className="load-older">
                    <button type="button" className="button secondary" onClick={() => void loadOlder()}>
                      Load earlier messages
                    </button>
                  </div>
                )}
                {olderError !== undefined && (
                  <ErrorNotice error={olderError} retry={() => void loadOlder()} />
                )}

                {visibleMessages.map((message) => (
                  <article
                    key={message.id}
                    className={
                      isInternal(message)
                        ? 'entry entry-internal'
                        : message.sender_type === 'CUSTOMER'
                          ? 'entry entry-customer'
                          : 'entry entry-support'
                    }
                  >
                    <header className="entry-head">
                      <strong>{message.display_name}</strong>
                      {isInternal(message) && <span className="internal-tag">Internal note</span>}
                      <span className="muted">
                        <Timestamp value={message.created_at} />
                      </span>
                    </header>
                    <p className="entry-body">{message.body}</p>
                    {(message.attachments ?? []).length > 0 && (
                      <ul className="attachment-list">
                        {(message.attachments ?? []).map((attachment) => (
                          <li key={attachment.id}>
                            <a
                              className="text-link"
                              href={`/api/v1/attachments/${attachment.id}/download`}
                            >
                              📎 {attachment.original_name}
                            </a>
                          </li>
                        ))}
                      </ul>
                    )}
                    {isInternal(message) && (
                      <p className="internal-hint">Only your support team can see this.</p>
                    )}
                  </article>
                ))}
              </div>

              {showNew && (
                <button
                  type="button"
                  className="button secondary new-replies"
                  onClick={() => {
                    const node = conversationRef.current;
                    if (node) node.scrollTop = node.scrollHeight;
                    atBottomRef.current = true;
                    setShowNew(false);
                  }}
                >
                  New replies ↓
                </button>
              )}

              {canCompose ? (
                <form
                  className="composer"
                  onSubmit={(event) => {
                    event.preventDefault();
                    void sendReply();
                  }}
                >
                  {staff && (
                    <div className="composer-modes" role="tablist" aria-label="Message visibility">
                      <button
                        type="button"
                        role="tab"
                        aria-selected={effectiveMode === 'PUBLIC'}
                        className={effectiveMode === 'PUBLIC' ? 'mode-button mode-active' : 'mode-button'}
                        onClick={() => setMode('PUBLIC')}
                        disabled={closed}
                      >
                        Public reply
                      </button>
                      <button
                        type="button"
                        role="tab"
                        aria-selected={effectiveMode === 'INTERNAL'}
                        className={effectiveMode === 'INTERNAL' ? 'mode-button mode-active' : 'mode-button'}
                        onClick={() => setMode('INTERNAL')}
                      >
                        Internal note
                      </button>
                    </div>
                  )}
                  {staff && savedReplies.length > 0 && (
                    <label className="saved-reply-picker">
                      <span className="visually-hidden">Insert a saved reply</span>
                      <select
                        value=""
                        onChange={(event) => {
                          const reply = savedReplies.find(
                            (item) => item.id === event.target.value,
                          );
                          if (!reply) return;
                          setDrafts((current) => ({
                            ...current,
                            [effectiveMode]: current[effectiveMode]
                              ? `${current[effectiveMode]}\n\n${reply.body}`
                              : reply.body,
                          }));
                          setSendKey(newIdempotencyKey());
                        }}
                      >
                        <option value="">Insert saved reply…</option>
                        {savedReplies.map((reply) => (
                          <option key={reply.id} value={reply.id}>
                            {reply.title}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                  <label className="composer-field">
                    <span className="visually-hidden">
                      {effectiveMode === 'INTERNAL' ? 'Internal note' : 'Public reply'}
                    </span>
                    <textarea
                      value={drafts[effectiveMode]}
                      onChange={(event) => {
                        setDrafts((current) => ({ ...current, [effectiveMode]: event.target.value }));
                        setSendKey(newIdempotencyKey());
                      }}
                      maxLength={10000}
                      rows={4}
                      placeholder={
                        effectiveMode === 'INTERNAL'
                          ? 'Add investigation notes visible only to the support team…'
                          : 'Write a reply the customer will see…'
                      }
                      disabled={closed && effectiveMode === 'PUBLIC'}
                    />
                  </label>
                  {effectiveMode === 'INTERNAL' && (
                    <p className="internal-hint">Internal notes are never shown to the customer.</p>
                  )}
                  {closed && staff && (
                    <p className="muted">
                      Closed tickets accept internal notes only until an agent reopens them.
                    </p>
                  )}
                  {sendError !== undefined && <ErrorNotice error={sendError} />}
                  <div className="composer-actions">
                    <label className="attach-control">
                      <span className="visually-hidden">Attach files</span>
                      <input
                        type="file"
                        multiple
                        accept="image/png,image/jpeg,application/pdf"
                        onChange={(event) => {
                          const selected = Array.from(event.target.files ?? []).slice(0, 3);
                          setPendingFiles(selected);
                          setSendKey(newIdempotencyKey());
                        }}
                      />
                    </label>
                    <button
                      type="submit"
                      disabled={sending || (!drafts[effectiveMode].trim() && pendingFiles.length === 0)}
                    >
                      {sending ? 'Sending…' : effectiveMode === 'INTERNAL' ? 'Add note' : 'Send reply'}
                    </button>
                    {sendError !== undefined && (
                      <button
                        type="button"
                        className="button secondary"
                        disabled={sending}
                        onClick={() => void sendReply()}
                      >
                        Retry
                      </button>
                    )}
                  </div>
                </form>
              ) : (
                <p className="closed-notice" role="status">
                  This ticket is closed. An agent must reopen it before a public reply can be added.
                </p>
              )}
            </div>

            <aside className="detail-sidebar">
              {isAgentTicket(ticket) && etag && (
                <MetadataForm
                  key={`${ticket.id}:${ticket.version}`}
                  ticket={ticket}
                  etag={etag}
                  agents={agents}
                  onSaved={() => setTicketReload((value) => value + 1)}
                />
              )}

              {ticket.order_id && <OrderContext key={ticket.order_id} orderId={ticket.order_id} />}

              {isAgentTicket(ticket) && (
                <SlaCard
                  key={`sla:${ticket.id}`}
                  ticketId={ticket.id}
                  refreshKey={`${ticket.id}:${ticket.version}:${revision}`}
                />
              )}

              <section className="card audit-card" aria-labelledby="audit-title">
                <h2 id="audit-title">{staff ? 'Audit history' : 'Status history'}</h2>
                {staff ? (
                  events.length === 0 ? (
                    <p className="muted">No recorded events yet.</p>
                  ) : (
                    <ol className="audit-list">
                      {events.map((event) => (
                        <li key={event.id}>
                          <span className="audit-event">{label(event.event_type)}</span>
                          {event.field && (
                            <span className="audit-change muted">
                              {event.old_value ? label(event.old_value) : '—'} →{' '}
                              {event.new_value ? label(event.new_value) : '—'}
                            </span>
                          )}
                          {event.reason && <span className="audit-reason muted">{event.reason}</span>}
                          <span className="muted">
                            <Timestamp value={event.created_at} />
                          </span>
                        </li>
                      ))}
                    </ol>
                  )
                ) : history.length === 0 ? (
                  <p className="muted">No status changes yet.</p>
                ) : (
                  <ol className="audit-list">
                    {history.map((event) => (
                      <li key={event.id}>
                        <span className="audit-event">
                          {event.new_value ? label(event.new_value) : label(event.event_type)}
                        </span>
                        <span className="muted">
                          <Timestamp value={event.created_at} />
                        </span>
                      </li>
                    ))}
                  </ol>
                )}
              </section>
            </aside>
          </div>
        </>
      )}
    </section>
  );
}
