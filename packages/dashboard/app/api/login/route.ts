/**
 * POST /api/login (Slice 5B).
 *
 * Constant-time-compared password against ``LUMIN_DASHBOARD_PASSWORD``;
 * on match, mints an HMAC-signed session cookie via
 * ``signSessionCookie`` and returns 200. On miss, 401. On unset env
 * var (auth disabled), 400 — there's no point hitting login when
 * auth is off.
 */

import { NextResponse } from 'next/server';
import {
  COOKIE_NAME,
  TWO_WEEKS_SECONDS,
  signSessionCookie,
} from '../../../middleware';

export const runtime = 'edge';

function constantTimeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) {
    // Still walk a constant pad to dampen length-based timing — but
    // mismatched length is an instant fail.
    let acc = 1;
    const len = Math.max(a.length, b.length);
    for (let i = 0; i < len; i++) {
      acc |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
    }
    void acc;
    return false;
  }
  let diff = 0;
  for (let i = 0; i < a.length; i++) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

export async function POST(req: Request) {
  const expected = process.env.LUMIN_DASHBOARD_PASSWORD?.trim();
  if (!expected) {
    return NextResponse.json(
      {
        error:
          'auth disabled (LUMIN_DASHBOARD_PASSWORD is unset on this Lumin instance)',
      },
      { status: 400 },
    );
  }

  let body: { password?: string } = {};
  try {
    body = (await req.json()) as { password?: string };
  } catch {
    return NextResponse.json({ error: 'bad request' }, { status: 400 });
  }

  const provided = (body.password ?? '').trim();
  if (!provided) {
    return NextResponse.json({ error: 'password required' }, { status: 400 });
  }
  if (!constantTimeEqual(provided, expected)) {
    return NextResponse.json({ error: 'invalid password' }, { status: 401 });
  }

  const { value, expires } = await signSessionCookie(expected);
  const res = NextResponse.json({ ok: true });
  res.cookies.set({
    name: COOKIE_NAME,
    value,
    httpOnly: true,
    secure: req.url.startsWith('https://'),
    sameSite: 'lax',
    path: '/',
    maxAge: TWO_WEEKS_SECONDS,
    expires,
  });
  return res;
}
