import { useEffect, useState } from 'react';
import { getSession, type Identity } from './api';

export function useLiveUpdates(user: Identity) {
  const [revision, setRevision] = useState(0);
  const [connection, setConnection] = useState<'connecting' | 'live' | 'reconnecting'>('connecting');
  useEffect(() => {
    let active = true;
    let source: EventSource | undefined;
    let refreshTimer: ReturnType<typeof setTimeout> | undefined;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
    let delay = 2000;
    const refresh = () => {
      if (!refreshTimer) refreshTimer = setTimeout(() => {
        refreshTimer = undefined;
        if (active) setRevision(value => value + 1);
      }, 80);
    };
    const checkIdentity = () => {
      void getSession().then(current => {
        if (active && (current.id !== user.id || current.role !== user.role)) {
          window.dispatchEvent(new Event('auth-refresh'));
        }
      }).catch(error => {
        if (active && error && typeof error === 'object' && 'status' in error && error.status === 401) {
          window.dispatchEvent(new Event('session-expired'));
        }
      });
    };
    const connect = () => {
      if (!active || typeof EventSource === 'undefined') return;
      source = new EventSource('/api/v1/events');
      source.onopen = () => { if (active) { setConnection('live'); delay = 2000; refresh(); } };
      source.addEventListener('resync.required', refresh);
      source.addEventListener('ticket.changed', refresh);
      source.addEventListener('queue.changed', refresh);
      source.addEventListener('auth.expired', () => window.dispatchEvent(new Event('session-expired')));
      source.onerror = () => {
        source?.close();
        if (!active) return;
        setConnection('reconnecting');
        checkIdentity();
        reconnectTimer = setTimeout(connect, delay);
        delay = Math.min(delay * 2, 15000);
      };
    };
    const focus = () => { refresh(); checkIdentity(); };
    const poll = setInterval(focus, 15000);
    window.addEventListener('focus', focus);
    window.addEventListener('online', focus);
    connect();
    return () => {
      active = false;
      source?.close();
      clearInterval(poll);
      clearTimeout(refreshTimer);
      clearTimeout(reconnectTimer);
      window.removeEventListener('focus', focus);
      window.removeEventListener('online', focus);
    };
  }, [user.id, user.role]);
  return { revision, connection };
}
