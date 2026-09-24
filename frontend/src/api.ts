import type {
  AgentSummary,
  AgentTicket,
  AttachmentDTO,
  CustomerSummary,
  CustomerTicket,
  DashboardSummary,
  MessagePage,
  PublicHistoryItem,
  PublicHistoryPage,
  PublicMessage,
  OrderSummary,
  PreferenceDTO,
  SavedReplyDTO,
  SlaTimerDTO,
  StaffMessage,
  TicketEventDTO,
  TicketEventPage,
  TicketList,
  TicketPriority,
  TicketSlaDTO,
  TicketStatus,
  Visibility,
} from './generated/api-types';

export type {
  AgentSummary,
  AgentTicket,
  AttachmentDTO,
  CustomerSummary,
  CustomerTicket,
  DashboardSummary,
  MessagePage,
  PublicHistoryItem,
  PublicHistoryPage,
  PublicMessage,
  OrderSummary,
  PreferenceDTO,
  SavedReplyDTO,
  SlaTimerDTO,
  StaffMessage,
  TicketEventDTO,
  TicketEventPage,
  TicketList,
  TicketPriority,
  TicketSlaDTO,
  TicketStatus,
  Visibility,
};

export type Ticket = CustomerTicket | AgentTicket;
export type TicketMessage = PublicMessage | StaffMessage;

export type Identity = {
  id: string;
  email: string;
  display_name: string;
  role: 'CUSTOMER' | 'AGENT' | 'ADMIN';
};

type CsrfResponse = {
  csrf_token: string;
};

type LoginResponse = Identity & CsrfResponse;

type ErrorEnvelope = {
  error?: {
    code?: unknown;
    message?: unknown;
    fields?: unknown;
  };
};

export type RequestOptions = {
  method?: string;
  json?: unknown;
  form?: FormData;
  headers?: Record<string, string>;
  signal?: AbortSignal;
};

type InternalRequestOptions = RequestOptions & {
  skipCsrf?: boolean;
  suppressSessionExpired?: boolean;
};

export class ApiError extends Error {
  status: number;
  code: string;
  fields: Record<string, string>;

  constructor(status: number, code: string, message: string, fields: Record<string, string> = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.fields = fields;
  }
}

const API_PREFIX = '/api/v1';
const REQUEST_TIMEOUT_MS = 12_000;
const MUTATION_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

let csrfToken: string | null = null;
let csrfInFlight: Promise<string> | null = null;
let csrfController: AbortController | null = null;
let csrfGeneration = 0;

function apiPath(path: string): string {
  if (path.startsWith('http://') || path.startsWith('https://') || path.startsWith(API_PREFIX)) {
    return path;
  }
  const normalized = path.startsWith('/') ? path : `/${path}`;
  return `${API_PREFIX}${normalized}`;
}

function parseHeaders(headers: Record<string, string> | undefined): Record<string, string> {
  return { ...(headers ?? {}) };
}

function isMutation(method: string): boolean {
  return MUTATION_METHODS.has(method.toUpperCase());
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function fieldsFrom(value: unknown): Record<string, string> {
  if (!isRecord(value)) return {};
  const result: Record<string, string> = {};
  for (const [key, fieldValue] of Object.entries(value)) {
    if (typeof fieldValue === 'string') {
      result[key] = fieldValue;
    }
  }
  return result;
}

async function readJson(response: Response): Promise<unknown> {
  if (response.status === 204) return undefined;
  const contentType = response.headers.get('Content-Type') ?? '';
  if (!contentType.toLowerCase().includes('application/json')) return undefined;
  try {
    return await response.json();
  } catch {
    return undefined;
  }
}

function errorFromResponse(response: Response, payload: unknown): ApiError {
  const envelope = isRecord(payload) ? (payload as ErrorEnvelope).error : undefined;
  const code = typeof envelope?.code === 'string' ? envelope.code : 'http_error';
  const message =
    typeof envelope?.message === 'string' && envelope.message.trim().length > 0
      ? envelope.message
      : response.status >= 500
        ? 'Service temporarily unavailable.'
        : 'Request failed.';
  return new ApiError(response.status, code, message, fieldsFrom(envelope?.fields));
}

function timeoutError(): ApiError {
  return new ApiError(0, 'request_timeout', 'The request timed out. Please try again.');
}

function abortedError(): ApiError {
  return new ApiError(0, 'request_aborted', 'The request was cancelled.');
}

function networkError(): ApiError {
  return new ApiError(0, 'network_error', 'Could not reach the API. Please try again.');
}

function makeAbortSignal(callerSignal: AbortSignal | undefined, timeoutMs: number) {
  const controller = new AbortController();
  let timedOut = false;
  const timeoutId = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);

  const abortFromCaller = () => controller.abort();
  if (callerSignal?.aborted) {
    controller.abort();
  } else {
    callerSignal?.addEventListener('abort', abortFromCaller, { once: true });
  }

  return {
    signal: controller.signal,
    didTimeOut: () => timedOut,
    cleanup: () => {
      window.clearTimeout(timeoutId);
      callerSignal?.removeEventListener('abort', abortFromCaller);
    },
  };
}

async function fetchCsrf(): Promise<string> {
  if (csrfToken) return csrfToken;
  if (csrfInFlight) return csrfInFlight;

  const generation = csrfGeneration;
  csrfController = new AbortController();
  csrfInFlight = requestInternal<CsrfResponse>('/auth/csrf', {
    method: 'GET',
    signal: csrfController.signal,
    skipCsrf: true,
    suppressSessionExpired: true,
  })
    .then(({ data }) => {
      if (generation !== csrfGeneration) {
        throw new ApiError(0, 'request_aborted', 'The request was cancelled.');
      }
      csrfToken = data.csrf_token;
      return data.csrf_token;
    })
    .finally(() => {
      if (generation === csrfGeneration) {
        csrfInFlight = null;
        csrfController = null;
      }
    });

  return csrfInFlight;
}

function dispatchSessionExpired(path: string, options: InternalRequestOptions): void {
  if (options.suppressSessionExpired) return;
  const normalizedPath = apiPath(path);
  if (normalizedPath.endsWith('/auth/me') || normalizedPath.endsWith('/auth/login')) return;
  window.dispatchEvent(new CustomEvent('session-expired'));
}

export async function request<T>(
  path: string,
  options: RequestOptions = {},
): Promise<{ data: T; headers: Headers }> {
  return requestInternal<T>(path, options);
}

async function requestInternal<T>(
  path: string,
  options: InternalRequestOptions = {},
): Promise<{ data: T; headers: Headers }> {
  const method = (options.method ?? 'GET').toUpperCase();
  const headers = parseHeaders(options.headers);

  if (options.json !== undefined) {
    headers['Content-Type'] = headers['Content-Type'] ?? 'application/json';
  }
  headers.Accept = headers.Accept ?? 'application/json';

  if (!options.skipCsrf && isMutation(method)) {
    headers['X-CSRF-Token'] = headers['X-CSRF-Token'] ?? (await fetchCsrf());
  }

  const abort = makeAbortSignal(options.signal, REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(apiPath(path), {
      method,
      headers,
      body:
        options.form !== undefined
          ? options.form
          : options.json === undefined
            ? undefined
            : JSON.stringify(options.json),
      credentials: 'same-origin',
      cache: 'no-store',
      signal: abort.signal,
    });
    const payload = await readJson(response);
    if (!response.ok) {
      if (response.status === 401) {
        csrfToken = null;
        dispatchSessionExpired(path, options);
      }
      throw errorFromResponse(response, payload);
    }
    return { data: payload as T, headers: response.headers };
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (abort.didTimeOut()) throw timeoutError();
    if (options.signal?.aborted || abort.signal.aborted) throw abortedError();
    throw networkError();
  } finally {
    abort.cleanup();
  }
}

export async function api<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { data } = await request<T>(path, options);
  return data;
}

export async function getSession(): Promise<Identity> {
  return api<Identity>('/auth/me', { method: 'GET' }).catch((error: unknown) => {
    if (error instanceof ApiError && error.status === 401) {
      throw error;
    }
    throw error;
  });
}

export async function login(email: string, password: string): Promise<Identity> {
  const response = await api<LoginResponse>('/auth/login', {
    method: 'POST',
    json: { email, password },
  });
  csrfToken = response.csrf_token;
  return {
    id: response.id,
    email: response.email,
    display_name: response.display_name,
    role: response.role,
  };
}

export async function logout(): Promise<void> {
  try {
    await api<void>('/auth/logout', { method: 'POST' });
  } finally {
    clearSession();
  }
}

export function clearSession(): void {
  csrfGeneration += 1;
  csrfToken = null;
  csrfInFlight = null;
  csrfController?.abort();
  csrfController = null;
}

type QueryValue = string | number | boolean | null | undefined;

export function queryString(params: Record<string, QueryValue>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '' || value === false) continue;
    search.set(key, String(value));
  }
  const encoded = search.toString();
  return encoded ? `?${encoded}` : '';
}

export function newIdempotencyKey(): string {
  const cryptoApi = globalThis.crypto;
  if (cryptoApi && typeof cryptoApi.randomUUID === 'function') {
    return cryptoApi.randomUUID();
  }
  return `idem-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

export type TicketListFilters = {
  status?: string;
  priority?: string;
  category?: string;
  query?: string;
  assigned_agent_id?: string;
  unassigned?: boolean;
  limit?: number;
  cursor?: string | null;
};

export async function listTickets(
  filters: TicketListFilters,
  signal?: AbortSignal,
): Promise<TicketList> {
  return api<TicketList>(`/tickets${queryString({ ...filters })}`, { method: 'GET', signal });
}

export type TicketCreateInput = {
  customer_email: string;
  category: string;
  subject: string;
  description: string;
  priority: string;
  order_id?: string | null;
  customer_id?: string | null;
};

export async function createTicket(
  input: TicketCreateInput,
  idempotencyKey: string,
): Promise<Ticket> {
  return api<Ticket>('/tickets', {
    method: 'POST',
    json: input,
    headers: { 'Idempotency-Key': idempotencyKey },
  });
}

export async function getTicket(
  ticketId: string,
  signal?: AbortSignal,
): Promise<{ ticket: Ticket; etag: string | null }> {
  const { data, headers } = await request<Ticket>(`/tickets/${ticketId}`, {
    method: 'GET',
    signal,
  });
  return { ticket: data, etag: headers.get('ETag') };
}

export type TicketPatchInput = {
  status?: string;
  priority?: string;
  assigned_agent_id?: string | null;
  reason?: string | null;
};

export async function patchTicket(
  ticketId: string,
  patch: TicketPatchInput,
  etag: string,
): Promise<{ ticket: AgentTicket; etag: string | null }> {
  const { data, headers } = await request<AgentTicket>(`/tickets/${ticketId}`, {
    method: 'PATCH',
    json: patch,
    headers: { 'If-Match': etag },
  });
  return { ticket: data, etag: headers.get('ETag') };
}

export async function listMessages(
  ticketId: string,
  params: { limit?: number; before?: string | null; after?: string | null } = {},
  signal?: AbortSignal,
): Promise<MessagePage> {
  return api<MessagePage>(`/tickets/${ticketId}/messages${queryString({ ...params })}`, {
    method: 'GET',
    signal,
  });
}

export type MessageInput = {
  body: string;
  visibility: 'PUBLIC' | 'INTERNAL';
  attachment_ids?: string[];
};

export async function createMessage(
  ticketId: string,
  input: MessageInput,
  idempotencyKey: string,
): Promise<TicketMessage> {
  return api<TicketMessage>(`/tickets/${ticketId}/messages`, {
    method: 'POST',
    json: input,
    headers: { 'Idempotency-Key': idempotencyKey },
  });
}

export async function listTicketEvents(
  ticketId: string,
  signal?: AbortSignal,
): Promise<TicketEventPage> {
  return api<TicketEventPage>(`/tickets/${ticketId}/events`, { method: 'GET', signal });
}

export async function listPublicHistory(
  ticketId: string,
  signal?: AbortSignal,
): Promise<PublicHistoryPage> {
  return api<PublicHistoryPage>(`/tickets/${ticketId}/public-history`, { method: 'GET', signal });
}

export async function listAgents(signal?: AbortSignal): Promise<AgentSummary[]> {
  const page = await api<{ items: AgentSummary[] }>('/agents', { method: 'GET', signal });
  return page.items;
}

export async function lookupCustomers(
  query: string,
  signal?: AbortSignal,
): Promise<CustomerSummary[]> {
  const page = await api<{ items: CustomerSummary[] }>(`/customers${queryString({ query })}`, {
    method: 'GET',
    signal,
  });
  return page.items;
}

export async function getOrder(orderId: string, signal?: AbortSignal): Promise<OrderSummary> {
  return api<OrderSummary>(`/mock/orders/${encodeURIComponent(orderId)}`, { method: 'GET', signal });
}

export async function getTicketSla(ticketId: string, signal?: AbortSignal): Promise<TicketSlaDTO> {
  return api<TicketSlaDTO>(`/tickets/${ticketId}/sla`, { method: 'GET', signal });
}

export async function listSavedReplies(signal?: AbortSignal): Promise<SavedReplyDTO[]> {
  const page = await api<{ items: SavedReplyDTO[] }>('/saved-replies', { method: 'GET', signal });
  return page.items;
}

export async function getDashboardSummary(signal?: AbortSignal): Promise<DashboardSummary> {
  return api<DashboardSummary>('/dashboard/summary', { method: 'GET', signal });
}

export async function markTicketSeen(ticketId: string): Promise<void> {
  await api<void>(`/tickets/${ticketId}/seen`, { method: 'PATCH' });
}

export async function uploadAttachment(ticketId: string, file: File): Promise<AttachmentDTO> {
  const form = new FormData();
  form.append('file', file, file.name);
  return api<AttachmentDTO>(`/tickets/${ticketId}/attachments`, { method: 'POST', form });
}

export async function getNotificationPreferences(signal?: AbortSignal): Promise<PreferenceDTO> {
  return api<PreferenceDTO>('/notification-preferences', { method: 'GET', signal });
}

export async function updateNotificationPreferences(emailOnReply: boolean): Promise<PreferenceDTO> {
  return api<PreferenceDTO>('/notification-preferences', {
    method: 'PATCH',
    json: { email_on_reply: emailOnReply },
  });
}
