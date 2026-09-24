import type { ReactNode } from 'react';
import { ApiError } from './api';

export const STATUSES = ['OPEN', 'IN_PROGRESS', 'WAITING_FOR_CUSTOMER', 'WAITING_FOR_PROVIDER', 'RESOLVED', 'CLOSED'] as const;
export const PRIORITIES = ['LOW', 'MEDIUM', 'HIGH', 'URGENT'] as const;
export const CATEGORIES = ['INSTALLATION', 'ACTIVATION', 'CONNECTIVITY', 'ORDER', 'TOPUP', 'REFUND', 'OTHER'] as const;
export function label(value: string) {
  const text = value.toLowerCase().replaceAll('_', ' ');
  return text[0]?.toUpperCase() + text.slice(1);
}
export function errorText(error: unknown): string {
  return error instanceof Error ? error.message : 'Something went wrong. Please try again.';
}
export function ErrorNotice({ error, retry }: { error: unknown; retry?: () => void }) {
  return <div className="error-notice" role="alert"><strong>{errorText(error)}</strong>
    {error instanceof ApiError && Object.entries(error.fields).length > 0 &&
      <ul>{Object.entries(error.fields).map(([field, message]) => <li key={field}>{label(field.replace('body.', ''))}: {message}</li>)}</ul>}
    {retry && <button className="button secondary" onClick={retry}>Try again</button>}
  </div>;
}
export function Badge({ value }: { value: string }) {
  return <span className={`badge badge-${value.toLowerCase()}`}>{label(value)}</span>;
}
export function Timestamp({ value }: { value: string }) {
  const date = new Date(value);
  return <time dateTime={value} title={date.toLocaleString()}>{date.toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
  })}</time>;
}
export function Field({ label: title, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return <label className="field"><span>{title}</span>{children}{hint && <small>{hint}</small>}</label>;
}
