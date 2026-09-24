import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { ApiError, clearSession, getSession, login, logout, type Identity } from './api';
import { HealthPage } from './HealthPage';
import { DashboardPage } from './DashboardPage';
import { SettingsNotificationsPage } from './SettingsNotificationsPage';
import { Link, navigate, useLocation } from './navigation';
import { ErrorNotice, Field } from './shared';
import { TicketList, TicketCreate } from './TicketPages';
import { TicketDetail } from './TicketDetail';
import { useLiveUpdates } from './useLiveUpdates';
import './styles.css';

function home(user: Identity) { return user.role === 'CUSTOMER' ? '/support' : '/agent/tickets'; }
function Brand() {
  return <Link className="brand" href="/" aria-label="Badi Support home"><img className="brand-logo" src="/badi-logo.png" alt="" width="795" height="267" /></Link>;
}
function SupportIcon({ kind }: { kind: 'message' | 'bell' | 'warning' }) {
  return <svg className={kind === 'warning' ? 'privacy-note-icon' : 'support-benefit-icon'} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {kind === 'message' && <><path d="M5 19l-1 3 4-2h9a4 4 0 0 0 4-4V7a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v8a4 4 0 0 0 2 4" /><path d="M8 9h8M8 13h6" /></>}
    {kind === 'bell' && <><path d="M10 5a2 2 0 0 1 4 0 7 7 0 0 1 4 6v3l2 2H4l2-2v-3a7 7 0 0 1 4-6" /><path d="M9 19a3 3 0 0 0 6 0" /></>}
    {kind === 'warning' && <><path d="M12 3 2.5 20h19L12 3Z" /><path d="M12 9v4M12 17h.01" /></>}
  </svg>;
}
function NavIcon({ kind }: { kind: 'tickets' | 'dashboard' | 'bell' | 'activity' }) {
  return <svg className="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {kind === 'tickets' && <><path d="M5 5h14v14H5z" /><path d="M8 9h8M8 13h8M8 17h5" /></>}
    {kind === 'dashboard' && <><path d="M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z" /></>}
    {kind === 'bell' && <><path d="M10 5a2 2 0 0 1 4 0 7 7 0 0 1 4 6v3l2 2H4l2-2v-3a7 7 0 0 1 4-6" /><path d="M9 19a3 3 0 0 0 6 0" /></>}
    {kind === 'activity' && <path d="M4 13h4l2-7 4 12 2-5h4" />}
  </svg>;
}
function LoginPage({ onLogin, notice }: { onLogin: (user: Identity) => void; notice: string }) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>();
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (pending) return;
    setPending(true); setError(undefined);
    try {
      const identity = await login(email, password);
      if (alive.current) { setPassword(''); onLogin(identity); }
    } catch (failure) { if (alive.current) { setError(failure); setPending(false); } }
  }
  return <div className="login-layout"><header className="login-header"><Brand /></header>
    <main className="login-main" id="main-content"><section className="login-story"><p className="eyebrow">Support</p>
      <h1>How can we help?</h1><p className="login-intro">eSIM questions, plan changes, and activation help — all in one place.</p>
      <div className="support-benefits">
        <article className="support-benefit"><SupportIcon kind="message" /><div><strong>One conversation</strong><p>Pick up where you left off. Your cases stay in one thread.</p></div></article>
        <article className="support-benefit"><SupportIcon kind="bell" /><div><strong>Direct updates</strong><p>Hear directly from the support team when your case moves forward.</p></div></article>
      </div>
      <div className="privacy-note"><SupportIcon kind="warning" /><p>Never share activation QR codes, passwords, or payment details in a support message.</p></div></section>
      <section className="login-card" aria-labelledby="login-title"><p className="eyebrow">Support</p><h2 id="login-title">Welcome back</h2><p className="login-card-intro">Sign in to pick up where you left off.</p>
        {notice && <p className="info-notice" role="status">{notice}</p>}
        <form onSubmit={submit}>
          <Field label="Email"><input type="email" placeholder="you@company.com" autoComplete="username" value={email} onChange={event => setEmail(event.target.value)} required maxLength={254} /></Field>
          <Field label="Password"><input type="password" placeholder="••••••••" autoComplete="current-password" value={password} onChange={event => setPassword(event.target.value)} required maxLength={1024} /></Field>
          {error !== undefined && <ErrorNotice error={error} />}
          <button className="button full" disabled={pending}>{pending ? 'Signing in…' : 'Sign in'}</button>
        </form><p className="login-footnote">Accounts are managed by your administrator. Public registration isn’t available.</p>
      </section></main><footer className="login-footer">Badi eSIM Support · Designed and developed by Saha Junior</footer></div>;
}

function Workspace({ user, location, onLogout }: { user: Identity; location: string; onLogout: () => Promise<void> }) {
  const staff = user.role !== 'CUSTOMER';
  const base = home(user);
  const pathname = location.split('?')[0];
  const { revision } = useLiveUpdates(user);
  const [loggingOut, setLoggingOut] = useState(false);
  const [logoutError, setLogoutError] = useState<unknown>();
  const isCreate = pathname === `${base}/new`;
  const isDashboard = staff && pathname === '/agent/dashboard';
  const isSystemStatus = staff && pathname === '/agent/system-status';
  const isSettings = pathname === '/settings/notifications';
  const match = pathname.match(staff ? /^\/agent\/tickets\/([0-9a-f-]{36})$/i : /^\/support\/tickets\/([0-9a-f-]{36})$/i);
  const isList = pathname === base;
  const isTickets = isList || match !== null;
  const detailBase = staff ? base : `${base}/tickets`;
  async function signOut() {
    setLoggingOut(true); setLogoutError(undefined);
    try { await onLogout(); } catch (error) { setLogoutError(error); setLoggingOut(false); }
  }
  return <div className="workspace"><a href="#main-content" className="skip-link">Skip to content</a>
    <header className="workspace-header"><Brand /><div className="identity"><span className={staff ? 'avatar avatar-staff' : 'avatar'} aria-hidden="true">{user.display_name.slice(0, 1).toUpperCase()}</span><span className="identity-name">{user.display_name}</span><span className="header-divider" aria-hidden="true" /><button className="text-button" disabled={loggingOut} onClick={() => void signOut()}>{loggingOut ? 'Signing out…' : 'Sign out'}</button></div></header>
    <div className="workspace-layout"><aside className="sidebar"><p className="eyebrow">Workspace</p><nav aria-label="Workspace"><Link href={base} className={isTickets ? 'nav-active' : ''}><NavIcon kind="tickets" />{staff ? 'Support queue' : 'My tickets'}</Link>{staff && <Link href="/agent/dashboard" className={isDashboard ? 'nav-active' : ''}><NavIcon kind="dashboard" />Dashboard</Link>}<Link href="/settings/notifications" className={isSettings ? 'nav-active' : ''}><NavIcon kind="bell" />Notifications</Link>{staff && <Link href="/agent/system-status" className={isSystemStatus ? 'nav-active' : ''}><NavIcon kind="activity" />System status</Link>}</nav></aside>
      <main id="main-content" className="workspace-main">
        {logoutError !== undefined && <ErrorNotice error={logoutError} />}
        {isList ? <TicketList key={user.id} user={user} base={base} detailBase={detailBase} location={location} revision={revision} /> :
          isCreate ? <TicketCreate user={user} base={base} detailBase={detailBase} /> :
            isDashboard ? <DashboardPage user={user} revision={revision} /> :
              isSystemStatus ? <HealthPage embedded /> :
              isSettings ? <SettingsNotificationsPage /> :
                match ? <TicketDetail key={match[1]} ticketId={match[1]} user={user} base={base} revision={revision} /> :
              <section className="empty-panel"><h1>{pathname.startsWith('/agent') && !staff ? 'Staff access required' : 'Page not found'}</h1><p>This page isn’t available to your account.</p><Link className="button" href={base}>Back to tickets</Link></section>}
      </main></div></div>;
}

export function App() {
  const location = useLocation();
  const [session, setSession] = useState<{ status: 'loading' | 'ready' | 'error'; user: Identity | null; error?: unknown }>({ status: 'loading', user: null });
  const [notice, setNotice] = useState('');
  const [generation, setGeneration] = useState(0);
  const sequence = useRef(0);
  const channel = useRef<BroadcastChannel | null>(null);
  const refreshSession = useCallback(() => {
    const current = ++sequence.current;
    void getSession().then(user => {
      if (current === sequence.current) setSession({ status: 'ready', user });
    }).catch(error => {
      if (current !== sequence.current) return;
      if (error instanceof ApiError && error.status === 401) setSession({ status: 'ready', user: null });
      else setSession({ status: 'error', user: null, error });
    });
  }, []);
  useEffect(() => {
    // /health is a public diagnostic screen: never probe the session or redirect.
    if (window.location.pathname.startsWith('/health')) return;
    refreshSession();
    const reset = () => { ++sequence.current; clearSession(); setGeneration(value => value + 1); setSession({ status: 'loading', user: null }); refreshSession(); };
    const expired = () => { ++sequence.current; clearSession(); setSession({ status: 'ready', user: null }); setGeneration(value => value + 1); setNotice('Your session ended. Sign in again.'); navigate('/login', true); };
    window.addEventListener('session-expired', expired);
    window.addEventListener('auth-refresh', reset);
    if (typeof BroadcastChannel !== 'undefined') { channel.current = new BroadcastChannel('badi-auth'); channel.current.onmessage = reset; }
    return () => { ++sequence.current; window.removeEventListener('session-expired', expired); window.removeEventListener('auth-refresh', reset); channel.current?.close(); };
  }, [refreshSession]);
  useEffect(() => {
    if (session.status !== 'ready' || location.startsWith('/health')) return;
    if (!session.user && location !== '/login') navigate('/login', true);
    else if (session.user && (location === '/' || location === '/login')) navigate(home(session.user), true);
  }, [session, location]);
  function signedIn(user: Identity) {
    ++sequence.current; setNotice(''); setGeneration(value => value + 1); setSession({ status: 'ready', user });
    channel.current?.postMessage({ type: 'auth-changed' }); navigate(home(user), true);
  }
  async function signedOut() {
    await logout(); ++sequence.current; clearSession(); setGeneration(value => value + 1); setSession({ status: 'ready', user: null });
    setNotice('You have signed out.'); channel.current?.postMessage({ type: 'auth-changed' }); navigate('/login', true);
  }
  if (location.split('?')[0] === '/health') return <HealthPage />;
  if (session.status === 'loading') return <main className="boot-screen"><p role="status">Opening your support workspace…</p></main>;
  if (session.status === 'error') return <main className="boot-screen"><h1>We couldn’t connect</h1><ErrorNotice error={session.error} retry={refreshSession} /><Link href="/health">Check system status</Link></main>;
  if (!session.user) return <LoginPage key={generation} onLogin={signedIn} notice={notice} />;
  return <Workspace key={`${session.user.id}:${generation}`} user={session.user} location={location} onLogout={signedOut} />;
}
