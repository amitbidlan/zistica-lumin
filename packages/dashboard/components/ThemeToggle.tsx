'use client';

import { useEffect, useState } from 'react';

/**
 * Header theme toggle.
 *
 * Resolution priority on first render:
 *   1. data-theme attribute on <html> (set by the inline script in
 *      layout.tsx before React hydrates — avoids FOUC)
 *   2. localStorage 'lumin.theme'
 *   3. prefers-color-scheme media query
 *   4. Default: dark
 *
 * The inline script in layout.tsx covers cases 1-3 server-side
 * already; this component just mirrors the current value into React
 * state and lets the user flip it. Click writes to localStorage AND
 * mirrors via data-theme so future page loads (and other tabs, on
 * the next render) pick up the change.
 *
 * The icon shows the *target* state (sun = "click to go to light",
 * moon = "click to go to dark") — most apps converge on this so
 * users don't have to guess what the current state is.
 */
type Theme = 'light' | 'dark';

const STORAGE_KEY = 'lumin.theme';


function readCurrentTheme(): Theme {
  if (typeof document === 'undefined') return 'dark';
  return document.documentElement.getAttribute('data-theme') === 'light'
    ? 'light'
    : 'dark';
}


export default function ThemeToggle() {
  // Start with `null` to avoid a hydration mismatch — the server
  // doesn't know the user's theme. After mount we sync from the
  // already-applied DOM attribute.
  const [theme, setTheme] = useState<Theme | null>(null);

  useEffect(() => {
    setTheme(readCurrentTheme());
  }, []);

  const toggle = () => {
    const next: Theme = theme === 'light' ? 'dark' : 'light';
    setTheme(next);
    document.documentElement.setAttribute('data-theme', next);
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* Safari private mode etc. — non-critical */
    }
  };

  // Until the effect runs, render a placeholder of identical size to
  // avoid layout shift. aria-hidden so screen readers don't announce
  // "button button" if React hydrates the real one immediately after.
  if (theme === null) {
    return (
      <span
        aria-hidden
        className="inline-block px-2 py-1"
        style={{ width: 28, height: 28 }}
      />
    );
  }

  const isDark = theme === 'dark';
  return (
    <button
      type="button"
      onClick={toggle}
      aria-label={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
      title={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
      className="px-2 py-1 rounded text-[var(--muted)] hover:text-[var(--foreground)] hover:bg-[var(--background-hover)] transition-colors"
    >
      {isDark ? <SunIcon /> : <MoonIcon />}
    </button>
  );
}


/**
 * Inline SVGs — keeps the dashboard free of an icon library dep
 * (lucide-react isn't installed; see issue #27 for the broader icon
 * conversation). 14×14 to match the header text size.
 */
function SunIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
    </svg>
  );
}


function MoonIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79Z" />
    </svg>
  );
}
