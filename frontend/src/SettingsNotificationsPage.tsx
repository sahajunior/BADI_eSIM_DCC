import { useEffect, useRef, useState } from 'react';
import { getNotificationPreferences, updateNotificationPreferences } from './api';
import { ErrorNotice } from './shared';

export function SettingsNotificationsPage() {
  const [state, setState] = useState<{
    status: 'loading' | 'ready' | 'error';
    emailOnReply: boolean;
    error?: unknown;
  }>({ status: 'loading', emailOnReply: true });
  const [saving, setSaving] = useState(false);
  const interacted = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    void getNotificationPreferences(controller.signal)
      .then((prefs) => {
        // A late load response must never clobber a user's change.
        if (!controller.signal.aborted && !interacted.current) {
          setState({ status: 'ready', emailOnReply: prefs.email_on_reply });
        }
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted && !interacted.current) {
          setState({ status: 'error', emailOnReply: true, error });
        }
      });
    return () => controller.abort();
  }, []);

  async function toggle(next: boolean) {
    if (saving) return;
    interacted.current = true;
    setSaving(true);
    setState((current) => ({ ...current, error: undefined, emailOnReply: next }));
    try {
      const updated = await updateNotificationPreferences(next);
      setState({ status: 'ready', emailOnReply: updated.email_on_reply });
    } catch (error) {
      setState({ status: 'ready', emailOnReply: !next, error });
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="page page-narrow settings-page" aria-labelledby="settings-title">
      <div className="page-head">
        <div>
          <h1 id="settings-title">Notifications</h1>
          <p>Control how you hear about ticket activity.</p>
        </div>
      </div>

      {state.status === 'loading' ? (
        <p className="muted" role="status">
          Loading preferences…
        </p>
      ) : (
        <div className="settings-card">
          <label className="setting-row">
            <span className="setting-info">
              <strong>Email me about replies</strong>
              <small>We send the ticket number and a sign-in link — never the message contents.</small>
            </span>
            <span className="toggle-control">
              <input
                type="checkbox"
                checked={state.emailOnReply}
                disabled={saving}
                onChange={(event) => void toggle(event.target.checked)}
              />
              <span className="toggle-slider" aria-hidden="true" />
            </span>
          </label>
          {state.error !== undefined && <ErrorNotice error={state.error} />}
        </div>
      )}
    </section>
  );
}
