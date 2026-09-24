import { useSyncExternalStore, type AnchorHTMLAttributes, type MouseEvent } from 'react';

const eventName = 'badi-navigation';
function subscribe(callback: () => void) {
  window.addEventListener('popstate', callback);
  window.addEventListener(eventName, callback);
  return () => {
    window.removeEventListener('popstate', callback);
    window.removeEventListener(eventName, callback);
  };
}
const snapshot = () => window.location.pathname + window.location.search;
export function useLocation() {
  return useSyncExternalStore(subscribe, snapshot);
}
export function navigate(path: string, replace = false) {
  if (!path.startsWith('/') || path.startsWith('//')) return;
  window.history[replace ? 'replaceState' : 'pushState'](null, '', path);
  window.dispatchEvent(new Event(eventName));
  window.scrollTo({ top: 0 });
}
export function Link({ href = '/', onClick, ...props }: AnchorHTMLAttributes<HTMLAnchorElement>) {
  function click(event: MouseEvent<HTMLAnchorElement>) {
    onClick?.(event);
    if (!event.defaultPrevented && event.button === 0 && !event.metaKey && !event.ctrlKey &&
      !event.altKey && !event.shiftKey && !props.target && href.startsWith('/')) {
      event.preventDefault();
      navigate(href);
    }
  }
  return <a {...props} href={href} onClick={click} />;
}
