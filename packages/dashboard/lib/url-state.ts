'use client';

/**
 * URL-backed state hooks.
 *
 * Local ``useState`` for dashboard filters resets every time the
 * operator refreshes the page — frustrating when they've narrowed a
 * grid down to "openclaw + ollama + last 7d" and just want to
 * re-fetch. These hooks source state from the URL search params
 * instead so refresh, deep links, and browser back/forward all
 * preserve the operator's filter set.
 *
 * Two flavors:
 *   - ``useUrlString(key, default)`` — string filter (e.g. project,
 *     provider, search box)
 *   - ``useUrlNumber(key, default)`` — numeric filter (e.g. activity
 *     window in hours)
 *   - ``useUrlBoolean(key, default)`` — boolean toggle (e.g. "only
 *     violated traces")
 *
 * Each behaves like ``useState`` but writes via ``router.replace``
 * (so the back stack doesn't fill with filter changes). Default
 * values are NEVER written to the URL — keeps the address bar tidy
 * for the no-filter common case.
 */

import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { useCallback } from 'react';


function setOrDelete(
  params: URLSearchParams,
  key: string,
  value: string,
  defaultValue: string,
): URLSearchParams {
  const out = new URLSearchParams(params.toString());
  if (!value || value === defaultValue) out.delete(key);
  else out.set(key, value);
  return out;
}


export function useUrlString(
  key: string,
  defaultValue: string,
): [string, (next: string) => void] {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const value = params.get(key) ?? defaultValue;
  const setValue = useCallback(
    (next: string) => {
      const updated = setOrDelete(params, key, next, defaultValue);
      const qs = updated.toString();
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    },
    [params, pathname, router, key, defaultValue],
  );
  return [value, setValue];
}


export function useUrlNumber(
  key: string,
  defaultValue: number,
): [number, (next: number) => void] {
  const [str, setStr] = useUrlString(key, String(defaultValue));
  const parsed = Number.parseInt(str, 10);
  const value = Number.isFinite(parsed) ? parsed : defaultValue;
  const setValue = useCallback((next: number) => setStr(String(next)), [setStr]);
  return [value, setValue];
}


export function useUrlBoolean(
  key: string,
  defaultValue: boolean,
): [boolean, (next: boolean) => void] {
  // Encode booleans as "1"/"0" so the URL stays terse. We treat any
  // non-"1" / non-"true" value as false — generous parsing for hand-
  // typed deep links.
  const def = defaultValue ? '1' : '0';
  const [str, setStr] = useUrlString(key, def);
  const value = str === '1' || str === 'true';
  const setValue = useCallback(
    (next: boolean) => setStr(next ? '1' : '0'),
    [setStr],
  );
  return [value, setValue];
}
