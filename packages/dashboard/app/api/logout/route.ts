/**
 * POST /api/logout (Slice 5B).
 *
 * Clears the session cookie and 200s. The dashboard's nav surface
 * exposes a logout button when LUMIN_DASHBOARD_PASSWORD is set
 * (detected via the x-lumin-auth header the middleware emits).
 */

import { NextResponse } from 'next/server';
import { COOKIE_NAME } from '../../../middleware';

export const runtime = 'edge';

export async function POST() {
  const res = NextResponse.json({ ok: true });
  res.cookies.set({
    name: COOKIE_NAME,
    value: '',
    httpOnly: true,
    sameSite: 'lax',
    path: '/',
    maxAge: 0,
  });
  return res;
}
