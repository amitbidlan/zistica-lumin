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
import { useCallback, useEffect, useState } from 'react';


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


/** Options shared by every URL-state hook. */
export interface UrlStateOptions {
  /** Persist the value to ``localStorage[storageKey]`` so the choice
   *  survives navigation through links that don't carry the URL
   *  query (e.g. the top nav). Read precedence is URL > localStorage
   *  > default — explicit deep links always win, so an operator-
   *  shared link renders exactly what it says. */
  storageKey?: string;
}


export function useUrlString(
  key: string,
  defaultValue: string,
  options: UrlStateOptions = {},
): [string, (next: string) => void] {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const { storageKey } = options;

  // Hydrate the sticky fallback after mount — reading localStorage on
  // the first render would diverge from the server-rendered HTML and
  // trip a hydration warning. Until the useEffect runs the fallback
  // is null, so the initial paint matches the server.
  const [sticky, setSticky] = useState<string | null>(null);
  useEffect(() => {
    if (!storageKey || typeof window === 'undefined') return;
    setSticky(window.localStorage.getItem(storageKey));
  }, [storageKey]);

  const value = params.get(key) ?? sticky ?? defaultValue;

  const setValue = useCallback(
    (next: string) => {
      const updated = setOrDelete(params, key, next, defaultValue);
      const qs = updated.toString();
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
      if (storageKey && typeof window !== 'undefined') {
        if (!next || next === defaultValue) {
          window.localStorage.removeItem(storageKey);
          setSticky(null);
        } else {
          window.localStorage.setItem(storageKey, next);
          setSticky(next);
        }
      }
    },
    [params, pathname, router, key, defaultValue, storageKey],
  );
  return [value, setValue];
}


export function useUrlNumber(
  key: string,
  defaultValue: number,
  options: UrlStateOptions = {},
): [number, (next: number) => void] {
  const [str, setStr] = useUrlString(key, String(defaultValue), options);
  const parsed = Number.parseInt(str, 10);
  const value = Number.isFinite(parsed) ? parsed : defaultValue;
  const setValue = useCallback((next: number) => setStr(String(next)), [setStr]);
  return [value, setValue];
}


export function useUrlBoolean(
  key: string,
  defaultValue: boolean,
  options: UrlStateOptions = {},
): [boolean, (next: boolean) => void] {
  // Encode booleans as "1"/"0" so the URL stays terse. We treat any
  // non-"1" / non-"true" value as false — generous parsing for hand-
  // typed deep links.
  const def = defaultValue ? '1' : '0';
  const [str, setStr] = useUrlString(key, def, options);
  const value = str === '1' || str === 'true';
  const setValue = useCallback(
    (next: boolean) => setStr(next ? '1' : '0'),
    [setStr],
  );
  return [value, setValue];
}
