import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, api, clearSession, getSession, login, logout, request } from './api';

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init.headers },
  });
}

function errorResponse(status: number, code: string, message: string, fields: Record<string, string> = {}) {
  return jsonResponse({ error: { code, message, fields, request_id: 'req_1' } }, { status });
}

const identity = {
  id: 'user-1',
  email: 'customer@example.test',
  display_name: 'Customer One',
  role: 'CUSTOMER' as const,
};

describe('api client', () => {
  afterEach(() => {
    clearSession();
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('uses the API prefix, same-origin credentials, no-store cache, and returns data plus headers', async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(jsonResponse({ ok: true }, { status: 200, headers: { 'X-Trace': 'trace-1' } })),
    );
    vi.stubGlobal('fetch', fetchMock);

    await expect(request<{ ok: boolean }>('/tickets')).resolves.toMatchObject({ data: { ok: true } });

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/tickets',
      expect.objectContaining({
        method: 'GET',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: expect.objectContaining({ Accept: 'application/json' }),
      }),
    );
    await expect(api<{ ok: boolean }>('/tickets')).resolves.toEqual({ ok: true });
  });

  it('deduplicates CSRF bootstrap for concurrent mutations and preserves caller idempotency headers', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ csrf_token: 'csrf-1' }))
      .mockResolvedValue(jsonResponse({ ok: true }, { status: 201 }));
    vi.stubGlobal('fetch', fetchMock);

    await Promise.all([
      api('/tickets/a/messages', {
        method: 'POST',
        json: { body: 'hello' },
        headers: { 'Idempotency-Key': 'reply-1' },
      }),
      api('/tickets/a/messages', {
        method: 'POST',
        json: { body: 'again' },
        headers: { 'Idempotency-Key': 'reply-2' },
      }),
    ]);

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/auth/csrf');
    expect(fetchMock.mock.calls[1]?.[1]?.headers).toMatchObject({
      'Content-Type': 'application/json',
      'X-CSRF-Token': 'csrf-1',
      'Idempotency-Key': 'reply-1',
    });
    expect(fetchMock.mock.calls[2]?.[1]?.headers).toMatchObject({
      'X-CSRF-Token': 'csrf-1',
      'Idempotency-Key': 'reply-2',
    });
  });

  it('rotates the CSRF token from login responses without exposing it in the identity', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ csrf_token: 'bootstrap-csrf' }))
      .mockResolvedValueOnce(jsonResponse({ ...identity, csrf_token: 'auth-csrf' }))
      .mockResolvedValueOnce(jsonResponse({ ok: true }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(login('customer@example.test', 'secret')).resolves.toEqual(identity);
    await api('/tickets/a/messages', { method: 'POST', json: { body: 'after login' } });

    expect(fetchMock.mock.calls[1]?.[0]).toBe('/api/v1/auth/login');
    expect(JSON.parse(String(fetchMock.mock.calls[1]?.[1]?.body))).toEqual({
      email: 'customer@example.test',
      password: 'secret',
    });
    expect(fetchMock.mock.calls[2]?.[1]?.headers).toMatchObject({ 'X-CSRF-Token': 'auth-csrf' });
  });

  it('maps HTTP error envelopes into safe ApiError details', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(errorResponse(422, 'validation_error', 'Invalid request.', { 'body.email': 'valid email required' })));

    await expect(api('/auth/login', { method: 'POST', json: { email: 'bad', password: '' } })).rejects.toMatchObject({
      status: 422,
      code: 'validation_error',
      message: 'Invalid request.',
      fields: { 'body.email': 'valid email required' },
    });
  });

  it('dispatches session-expired for protected 401s but not initial getSession noise', async () => {
    const dispatchSpy = vi.spyOn(window, 'dispatchEvent');
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(errorResponse(401, 'unauthenticated', 'Authentication required.'))
      .mockResolvedValueOnce(errorResponse(401, 'unauthenticated', 'Authentication required.'));
    vi.stubGlobal('fetch', fetchMock);

    await expect(getSession()).rejects.toBeInstanceOf(ApiError);
    expect(dispatchSpy).not.toHaveBeenCalled();

    await expect(api('/tickets')).rejects.toBeInstanceOf(ApiError);
    expect(dispatchSpy).toHaveBeenCalledWith(expect.objectContaining({ type: 'session-expired' }));
  });

  it('does not blindly retry failed mutations', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ csrf_token: 'csrf-1' }))
      .mockResolvedValueOnce(errorResponse(503, 'service_unavailable', 'Service temporarily unavailable.'));
    vi.stubGlobal('fetch', fetchMock);

    await expect(api('/tickets/a/messages', { method: 'POST', json: { body: 'once' } })).rejects.toMatchObject({ status: 503 });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('clears and prevents stale in-flight CSRF tokens from being used after logout', async () => {
    let resolveCsrf!: (response: Response) => void;
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      if (String(input).endsWith('/auth/csrf')) {
        return new Promise<Response>((resolve) => {
          resolveCsrf = resolve;
        });
      }
      return Promise.resolve(new Response(null, { status: 204 }));
    });
    vi.stubGlobal('fetch', fetchMock);

    const firstMutation = api('/tickets/a/messages', { method: 'POST', json: { body: 'stale' } });
    await Promise.resolve();
    clearSession();
    resolveCsrf(jsonResponse({ csrf_token: 'stale-csrf' }));
    await expect(firstMutation).rejects.toThrow(/cancelled/i);

    const fetchMock2 = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ csrf_token: 'fresh-csrf' }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    vi.stubGlobal('fetch', fetchMock2);

    await logout();
    expect(fetchMock2.mock.calls[1]?.[0]).toBe('/api/v1/auth/logout');
    expect(fetchMock2.mock.calls[1]?.[1]?.headers).toMatchObject({ 'X-CSRF-Token': 'fresh-csrf' });
  });

  it('honors caller abort signals', async () => {
    const controller = new AbortController();
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    const result = api('/tickets', { signal: controller.signal });
    controller.abort();

    await expect(result).rejects.toMatchObject({ code: 'request_aborted' });
  });

  it('aborts slow requests after the bounded timeout', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    const result = api('/tickets');
    const expectation = expect(result).rejects.toMatchObject({
      code: 'request_timeout',
      status: 0,
    });
    await vi.advanceTimersByTimeAsync(12_001);

    await expectation;
  });
});
