import { act, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Identity } from './api';
import { useLiveUpdates } from './useLiveUpdates';

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  private listeners = new Map<string, Array<() => void>>();

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: () => void) {
    const existing = this.listeners.get(type) ?? [];
    this.listeners.set(type, [...existing, listener]);
  }

  close() {
    this.closed = true;
  }

  emit(type: string) {
    for (const listener of this.listeners.get(type) ?? []) listener();
  }

  open() {
    this.onopen?.();
  }

  fail() {
    this.onerror?.();
  }
}

const user: Identity = {
  id: 'cust-1',
  email: 'customer1@example.test',
  display_name: 'Customer One',
  role: 'CUSTOMER',
};

function Probe() {
  const { revision, connection } = useLiveUpdates(user);
  return (
    <div>
      <span data-testid="connection">{connection}</span>
      <span data-testid="revision">{revision}</span>
    </div>
  );
}

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.stubGlobal('EventSource', FakeEventSource);
  vi.stubGlobal(
    'fetch',
    vi.fn(() => Promise.resolve(new Response(JSON.stringify(user), { status: 200 }))),
  );
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('useLiveUpdates', () => {
  it('reports connecting then live and coalesces invalidations into a revision bump', async () => {
    render(<Probe />);

    expect(screen.getByTestId('connection')).toHaveTextContent('connecting');
    const source = FakeEventSource.instances[0]!;
    expect(source.url).toBe('/api/v1/events');

    act(() => source.open());
    expect(screen.getByTestId('connection')).toHaveTextContent('live');
    expect(screen.getByTestId('revision')).toHaveTextContent('0');

    act(() => {
      source.emit('resync.required');
      source.emit('ticket.changed');
      source.emit('queue.changed');
    });
    await waitFor(() => expect(screen.getByTestId('revision')).toHaveTextContent('1'));
  });

  it('moves to reconnecting on error and back to live after a successful reconnect', async () => {
    vi.useFakeTimers();
    render(<Probe />);
    const first = FakeEventSource.instances[0]!;
    act(() => first.open());
    expect(screen.getByTestId('connection')).toHaveTextContent('live');

    act(() => first.fail());
    expect(first.closed).toBe(true);
    expect(screen.getByTestId('connection')).toHaveTextContent('reconnecting');

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_001);
    });
    expect(FakeEventSource.instances).toHaveLength(2);
    const second = FakeEventSource.instances[1]!;
    act(() => second.open());
    expect(screen.getByTestId('connection')).toHaveTextContent('live');
    expect(screen.getByTestId('revision')).toHaveTextContent('1');
  });

  it('closes the stream on unmount', () => {
    const view = render(<Probe />);
    const source = FakeEventSource.instances[0]!;
    view.unmount();
    expect(source.closed).toBe(true);
  });
});
