/**
 * Login page (Slice 5B).
 *
 * Rendered when ``LUMIN_DASHBOARD_PASSWORD`` is set on the server
 * and the user lacks a valid session cookie. Single password field
 * — Lumin doesn't have user accounts in this slice. Successful
 * POST to /api/login lands a 14-day signed session cookie and
 * redirects to ?next= or "/" if absent.
 *
 * Intentionally minimal: no "remember me" checkbox (we always
 * remember 14 days), no password reset flow (this is a single
 * shared secret rotated via env), no SSO. All of those are
 * deliberate pin-points for the next slice.
 */

'use client';

import { Suspense, useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';

function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const next = params.get('next') ?? '/';
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const resp = await fetch('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password }),
      });
      if (resp.status === 200) {
        // Hard navigation — needs to round-trip middleware so the
        // new cookie is honored.
        window.location.href = next.startsWith('/') ? next : '/';
        return;
      }
      const body = (await resp.json().catch(() => ({}))) as { error?: string };
      setError(body.error ?? 'Login failed.');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Network error.');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="min-h-screen flex items-center justify-center p-6 bg-[var(--background)]">
      <form
        onSubmit={onSubmit}
        className="w-full max-w-sm space-y-4 card p-6 border border-[var(--border)]"
      >
        <div className="text-center mb-2">
          <div className="font-semibold text-lg">Lumin</div>
          <div className="text-xs text-[var(--muted)] mt-1">
            Local-first AI agent observability
          </div>
        </div>
        <label className="block text-sm">
          <span className="text-[var(--muted)] uppercase tracking-wider text-[10px] block mb-1">
            Password
          </span>
          <input
            autoFocus
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="w-full bg-[var(--background-raised)] border border-[var(--border)] rounded px-3 py-2 text-sm focus:outline-none focus:border-[var(--accent)]"
            disabled={submitting}
          />
        </label>
        {error ? (
          <div className="text-rose-300 text-xs border border-rose-500/30 bg-rose-500/[0.05] rounded px-2 py-1.5">
            {error}
          </div>
        ) : null}
        <button
          type="submit"
          disabled={submitting || !password}
          className="w-full pill bg-[var(--accent)] text-[var(--accent-foreground)] disabled:opacity-50"
        >
          {submitting ? 'Signing in…' : 'Sign in'}
        </button>
        <div className="text-[10px] text-[var(--muted)] text-center">
          Set via <code className="font-mono">LUMIN_DASHBOARD_PASSWORD</code>
        </div>
      </form>
    </main>
  );
}

export default function LoginPage() {
  return (
    <Suspense
      fallback={
        <main className="min-h-screen flex items-center justify-center text-[var(--muted)]">
          Loading…
        </main>
      }
    >
      <LoginForm />
    </Suspense>
  );
}
