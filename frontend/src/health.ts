export type ReadyHealth = {
  status: 'ready';
  database: 'ok';
};

export type UnavailableHealth = {
  status: 'unavailable';
  database: 'unavailable';
};

export type HealthResponse = ReadyHealth | UnavailableHealth;

export type HealthState =
  | { phase: 'loading' }
  | { phase: 'ready'; data: ReadyHealth }
  | { phase: 'unavailable'; data?: UnavailableHealth; reason: string };

export const HEALTH_ENDPOINT = '/api/v1/health/ready';
export const REQUEST_TIMEOUT_MS = 5_000;

function isReadyHealth(value: unknown): value is ReadyHealth {
  return (
    typeof value === 'object' &&
    value !== null &&
    'status' in value &&
    'database' in value &&
    value.status === 'ready' &&
    value.database === 'ok'
  );
}

function isUnavailableHealth(value: unknown): value is UnavailableHealth {
  return (
    typeof value === 'object' &&
    value !== null &&
    'status' in value &&
    'database' in value &&
    value.status === 'unavailable' &&
    value.database === 'unavailable'
  );
}

export async function fetchHealth(
  fetcher: typeof fetch = fetch,
  timeoutMs = REQUEST_TIMEOUT_MS,
): Promise<HealthState> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetcher(HEALTH_ENDPOINT, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      signal: controller.signal,
    });

    let payload: unknown = undefined;
    try {
      payload = await response.json();
    } catch {
      payload = undefined;
    }

    if (response.ok && isReadyHealth(payload)) {
      return { phase: 'ready', data: payload };
    }

    if (response.status === 503 && isUnavailableHealth(payload)) {
      return {
        phase: 'unavailable',
        data: payload,
        reason: 'The API answered, but the database is not ready yet.',
      };
    }

    return {
      phase: 'unavailable',
      reason: 'The health check returned an unexpected response.',
    };
  } catch (error) {
    const reason =
      error instanceof DOMException && error.name === 'AbortError'
        ? 'The health check timed out. Please try again.'
        : 'The health check could not reach the API. Please try again.';

    return { phase: 'unavailable', reason };
  } finally {
    window.clearTimeout(timeout);
  }
}
