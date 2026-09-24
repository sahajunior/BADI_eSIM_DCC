import { useCallback, useEffect, useState } from 'react';
import { api } from './api';

export function useResource<T>(path: string, revision = 0) {
  const [state, setState] = useState<{ path: string; data?: T; error?: unknown; loading: boolean }>({ path, loading: true });
  const [retry, setRetry] = useState(0);
  const reload = useCallback(() => setRetry(value => value + 1), []);
  useEffect(() => {
    const controller = new AbortController();
    void api<T>(path, { signal: controller.signal }).then(data => {
      if (!controller.signal.aborted) setState({ path, data, loading: false });
    }).catch(error => {
      if (!controller.signal.aborted) setState(old => ({ path, data: old.path === path ? old.data : undefined, error, loading: false }));
    });
    return () => controller.abort();
  }, [path, revision, retry]);
  // A new URL must never render data belonging to its previous resource.
  return { ...(state.path === path ? state : { path, loading: true }), reload };
}
