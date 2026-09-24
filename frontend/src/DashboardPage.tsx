import { useEffect, useState } from 'react';
import { getDashboardSummary, type DashboardSummary, type Identity } from './api';
import { Link } from './navigation';
import { ErrorNotice, label } from './shared';

function Metric({ heading, value, alert = false }: { heading: string; value: number | string; alert?: boolean }) {
  return (
    <div className="metric">
      <span className={alert ? 'metric-value metric-value-alert' : 'metric-value'}>{value}</span>
      <span className="metric-heading">{heading}</span>
    </div>
  );
}

function BarChart({ groups, kind }: { groups: { key: string; count: number }[]; kind: 'status' | 'priority' }) {
  const maximum = Math.max(1, ...groups.map((group) => group.count));

  return (
    <ul className="dashboard-bars">
      {groups.map((group) => {
        const title = label(group.key);
        const width = group.count === 0 ? 0 : Math.max(3, (group.count / maximum) * 100);
        return (
          <li key={group.key} className="dashboard-bar-row">
            <span className="dashboard-bar-label">{title}</span>
            <span className="dashboard-bar-track" role="img" aria-label={`${title}: ${group.count}`}>
              <span
                className={`dashboard-bar-fill dashboard-bar-${kind}-${group.key.toLowerCase()}`}
                style={{ width: `${width}%` }}
              />
            </span>
            <span className="dashboard-bar-value">{group.count}</span>
          </li>
        );
      })}
    </ul>
  );
}

function TimerRow({ heading, counts }: { heading: string; counts: { met: number; breached: number; pending: number } }) {
  return (
    <div className="timer-row">
      <span className="muted">{heading}</span>
      <span>
        <strong className="sla-met">{counts.met}</strong> met
        {' · '}
        <strong className="sla-overdue">{counts.breached}</strong> breached
        {' · '}
        <strong>{counts.pending}</strong> pending
      </span>
    </div>
  );
}

export function DashboardPage({ user, revision }: { user: Identity; revision: number }) {
  const [state, setState] = useState<{
    status: 'loading' | 'ready' | 'error';
    summary?: DashboardSummary;
    error?: unknown;
  }>({ status: 'loading' });
  const [reload, setReload] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    void getDashboardSummary(controller.signal)
      .then((summary) => {
        if (!controller.signal.aborted) setState({ status: 'ready', summary });
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) setState({ status: 'error', error });
      });
    return () => controller.abort();
  }, [revision, reload]);

  return (
    <section className="page dashboard-page" aria-labelledby="dashboard-title">
      <div className="page-head">
        <h1 id="dashboard-title">Dashboard</h1>
        {user.role === 'ADMIN' && (
          <Link className="button secondary" href="/agent/tickets">
            Open queue
          </Link>
        )}
      </div>

      {state.status === 'loading' && (
        <p className="muted" role="status">
          Loading dashboard…
        </p>
      )}
      {state.status === 'error' && (
        <ErrorNotice error={state.error} retry={() => setReload((value) => value + 1)} />
      )}

      {state.status === 'ready' && state.summary && (
        <>
          <div className="metric-grid">
            <Metric heading="Open tickets" value={state.summary.open_tickets} />
            <Metric heading="Unassigned" value={state.summary.unassigned} />
            <Metric heading="Overdue" value={state.summary.overdue} alert />
            <Metric heading="Total tickets" value={state.summary.total_tickets} />
          </div>

          <div className="dashboard-columns">
            <section className="card" aria-labelledby="by-status-title">
              <h2 id="by-status-title">Open by status</h2>
              {state.summary.by_status.length === 0 ? (
                <p className="muted">No tickets yet.</p>
              ) : (
                <BarChart groups={state.summary.by_status} kind="status" />
              )}
            </section>

            <section className="card" aria-labelledby="by-priority-title">
              <h2 id="by-priority-title">Open by priority</h2>
              {state.summary.by_priority.length === 0 ? (
                <p className="muted">Nothing open.</p>
              ) : (
                <BarChart groups={state.summary.by_priority} kind="priority" />
              )}
            </section>

            <section className="card dashboard-timers" aria-labelledby="timers-title">
              <h2 id="timers-title">Service levels</h2>
              <TimerRow heading="First response" counts={state.summary.first_response} />
              <TimerRow heading="Resolution" counts={state.summary.resolution} />
              <p className="muted sla-footnote">Policy {state.summary.policy_version}</p>
            </section>
          </div>
        </>
      )}
    </section>
  );
}
