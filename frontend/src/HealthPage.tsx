import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchHealth, HEALTH_ENDPOINT, type HealthState } from './health';
import './styles.css';

function statusText(state: HealthState): string {
  switch (state.phase) {
    case 'loading':
      return 'Checking services…';
    case 'ready':
      return 'Ready';
    case 'unavailable':
      return 'Services unavailable';
  }
}

function statusDescription(state: HealthState): string {
  switch (state.phase) {
    case 'loading':
      return 'Contacting the API readiness endpoint. Success is not assumed until the server confirms it.';
    case 'ready':
      return 'The API is reachable and PostgreSQL connectivity is confirmed by the backend.';
    case 'unavailable':
      return state.reason;
  }
}

export function HealthPage({ embedded = false }: { embedded?: boolean }) {
  const [healthState, setHealthState] = useState<HealthState>({ phase: 'loading' });
  const requestIdRef = useRef(0);

  const checkHealth = useCallback(() => {
    requestIdRef.current += 1;
    const requestId = requestIdRef.current;
    setHealthState({ phase: 'loading' });

    void fetchHealth().then((nextState) => {
      if (requestId === requestIdRef.current) {
        setHealthState(nextState);
      }
    });
  }, []);

  useEffect(() => {
    requestIdRef.current += 1;
    const requestId = requestIdRef.current;

    void fetchHealth().then((nextState) => {
      if (requestId === requestIdRef.current) {
        setHealthState(nextState);
      }
    });

    return () => {
      if (requestId === requestIdRef.current) {
        requestIdRef.current += 1;
      }
    };
  }, []);

  const isLoading = healthState.phase === 'loading';
  const apiStatus =
    healthState.phase === 'ready' || (healthState.phase === 'unavailable' && healthState.data)
      ? 'Reachable'
      : isLoading
        ? 'Checking'
        : 'Unavailable';
  const databaseStatus =
    healthState.phase === 'ready' ? 'PostgreSQL ok' : isLoading ? 'Checking' : 'Unavailable';

  const content = (
    <>
      {!embedded && (
        <a className="status-back" href="/">
          <span aria-hidden="true">←</span> Back to support
        </a>
      )}

      <p className="status-section-label">System status</p>
      <h1 id="page-title">Service health</h1>
      <p className="status-subtitle">Live readiness check for the API and database layer.</p>

      <section className="health-card" aria-labelledby="health-title">
        <div className="health-card__header">
          <h2 id="health-title">Readiness check</h2>
          <span className={`status-pill status-pill--${healthState.phase}`} aria-live="polite">
            {statusText(healthState)}
          </span>
        </div>

        <p className="endpoint">
          <span aria-hidden="true">⌁</span> <code>{HEALTH_ENDPOINT}</code>
        </p>

        <dl className="health-grid" aria-live="polite" aria-atomic="true">
          <div>
            <dt>API</dt>
            <dd>
              <span className={`health-dot health-dot--${healthState.phase}`} aria-hidden="true" />
              {apiStatus}
            </dd>
          </div>
          <div>
            <dt>Database</dt>
            <dd>
              <span className={`health-dot health-dot--${healthState.phase}`} aria-hidden="true" />
              {databaseStatus}
            </dd>
          </div>
        </dl>

        <p className="health-message">{statusDescription(healthState)}</p>

        <div className="actions">
          <button type="button" onClick={checkHealth} disabled={isLoading}>
            {isLoading ? 'Checking…' : 'Check again'}
          </button>
          <a href="/api/v1/docs" target="_blank" rel="noreferrer">
            API docs <span aria-hidden="true">↗</span>
          </a>
        </div>
      </section>

      <section className="scope-note" aria-labelledby="scope-title">
        <h2 id="scope-title">Phase 0 scope</h2>
        <p>
          This page checks the web shell, API boundary, and database readiness only. Sign-in and
          ticket workflows are introduced in later phases.
        </p>
      </section>
    </>
  );

  if (embedded) {
    return (
      <section className="status-page status-page-embedded" aria-labelledby="page-title">
        {content}
      </section>
    );
  }

  return (
    <main className="status-page" id="main-content">
      {content}
    </main>
  );
}
