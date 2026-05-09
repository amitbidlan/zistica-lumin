/**
 * Dashboard auth middleware (Slice 5B).
 *
 * Default-off. When ``LUMIN_DASHBOARD_PASSWORD`` is unset, this is a
 * no-op — the dashboard behaves exactly like it did before, no
 * login wall, no cookie. Existing localhost-dev workflows are
 * unchanged.
 *
 * When set, every page request needs a valid ``lumin_session``
 * cookie. Missing or invalid → redirect to /login with ?next=
 * carrying the original URL so the user lands back where they
 * tried to go.
 *
 * The session cookie is HMAC-signed with a secret derived from the
 * password itself (``hmac(password, "lumin-dashboard-session-v1")``).
 * Same password across restarts → same secret → cookies survive
 * restarts, which is the operator's expectation. Rotating the
 * password invalidates every active cookie automatically — also
 * the operator's expectation.
 *
 * The API layer's bearer token (LUMIN_API_TOKEN) is independent.
 * Operators commonly set both — the dashboard's /api/* rewrite
 * proxy injects the API token server-side so the browser never
 * sees it.
 */

import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

const COOKIE_NAME = 'lumin_session';
const SESSION_VERSION = 'v1';
const TWO_WEEKS_SECONDS = 60 * 60 * 24 * 14;

// Paths that bypass the auth check entirely. ``/login`` so the
// login flow itself can render; ``/_next`` for static assets that
// the login page depends on; ``/api/login`` so the password POST
// reaches the handler; ``/health`` (Next.js doesn't usually serve
// this but if anything else mounts it we keep it open).
const PUBLIC_PREFIXES = ['/login', '/_next', '/api/login', '/api/logout', '/health', '/icon.svg', '/favicon.ico'];

function isPublicPath(pathname: string): boolean {
  return PUBLIC_PREFIXES.some((p) => pathname === p || pathname.startsWith(p + '/') || pathname.startsWith(p));
}

export async function middleware(req: NextRequest) {
  const password = process.env.LUMIN_DASHBOARD_PASSWORD?.trim();
  if (!password) {
    // Auth disabled — pass through unchanged. The header below lets
    // the dashboard render an "auth: off" badge without a separate
    // /api/auth-status round trip.
    const res = NextResponse.next();
    res.headers.set('x-lumin-auth', 'off');
    return res;
  }

  const { pathname, search } = req.nextUrl;
  if (isPublicPath(pathname)) {
    return NextResponse.next();
  }

  const cookie = req.cookies.get(COOKIE_NAME)?.value;
  if (cookie && (await verifySessionCookie(cookie, password))) {
    const res = NextResponse.next();
    res.headers.set('x-lumin-auth', 'on');
    return res;
  }

  // Redirect to /login, carrying the original URL so we can land
  // the user back on the page they tried to reach.
  const loginUrl = req.nextUrl.clone();
  loginUrl.pathname = '/login';
  loginUrl.search = `?next=${encodeURIComponent(pathname + search)}`;
  return NextResponse.redirect(loginUrl);
}


// ---- session cookie HMAC -------------------------------------------------
//
// Edge-runtime-friendly: uses the global Web Crypto API (no node:crypto
// or string-comparison shortcuts). The cookie value is
// ``v1.<expires_at_unix_seconds>.<hex_hmac>``.

const enc = new TextEncoder();

async function deriveKey(password: string): Promise<CryptoKey> {
  const material = enc.encode(`lumin-dashboard-session-${SESSION_VERSION}::${password}`);
  return crypto.subtle.importKey(
    'raw', material, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign', 'verify'],
  );
}

async function signSessionCookie(password: string): Promise<{ value: string; expires: Date }> {
  const expiresAt = Math.floor(Date.now() / 1000) + TWO_WEEKS_SECONDS;
  const payload = `${SESSION_VERSION}.${expiresAt}`;
  const key = await deriveKey(password);
  const sigBuf = await crypto.subtle.sign('HMAC', key, enc.encode(payload));
  const sigHex = Array.from(new Uint8Array(sigBuf))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
  return {
    value: `${payload}.${sigHex}`,
    expires: new Date(expiresAt * 1000),
  };
}

async function verifySessionCookie(cookie: string, password: string): Promise<boolean> {
  const parts = cookie.split('.');
  if (parts.length !== 3) return false;
  const [version, expiresStr, sigHex] = parts;
  if (version !== SESSION_VERSION) return false;
  const expires = parseInt(expiresStr, 10);
  if (!Number.isFinite(expires) || expires < Math.floor(Date.now() / 1000)) {
    return false;
  }
  try {
    const key = await deriveKey(password);
    const sig = hexToBytes(sigHex);
    if (!sig) return false;
    return await crypto.subtle.verify(
      'HMAC', key, sig, enc.encode(`${version}.${expiresStr}`),
    );
  } catch {
    return false;
  }
}

function hexToBytes(hex: string): Uint8Array | null {
  if (!/^[0-9a-f]+$/i.test(hex) || hex.length % 2 !== 0) return null;
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) {
    out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  }
  return out;
}


// ---- exports for the API routes to share -------------------------------
// Importing from a middleware file works because Next.js bundles the
// middleware separately from app routes — the API route can import
// signSessionCookie via a relative path, both ending up in the
// edge runtime that supports Web Crypto.
export { signSessionCookie, COOKIE_NAME, TWO_WEEKS_SECONDS };


// Match every path except Next's internal ones. The middleware
// itself early-returns on /login etc., but the matcher still has to
// invoke it so the function gets the chance to redirect.
export const config = {
  matcher: ['/((?!_next/static|_next/image|favicon.ico).*)'],
};
