import './globals.css';
import type { Metadata } from 'next';
import Link from 'next/link';
import Logo from '@/components/Logo';
import ViolationsNavLink from '@/components/ViolationsNavLink';
import NavLink from '@/components/NavLink';
import PanicBanner from '@/components/PanicBanner';
import ThemeToggle from '@/components/ThemeToggle';

// Pre-hydration script — sets data-theme on <html> BEFORE React
// hydrates so light-mode users don't see a dark flash on first paint.
// Resolution: localStorage > prefers-color-scheme > default 'dark'.
// Wrapped in a try/catch so a Safari-private-mode localStorage
// throw doesn't blank the page.
const THEME_INIT_SCRIPT = `(function(){try{var s=localStorage.getItem('lumin.theme');var p=window.matchMedia('(prefers-color-scheme: light)').matches;var t=s||(p?'light':'dark');if(t==='light'){document.documentElement.setAttribute('data-theme','light');}}catch(e){}})();`;

export const metadata: Metadata = {
  title: {
    default: 'Lumin — local-first AI agent observability',
    template: '%s · Lumin',
  },
  description:
    'Add 2 lines of code, see every step your agent takes. Runs entirely on your laptop. No account, no credit card, no telemetry — Lumin never ships your traces anywhere.',
  applicationName: 'Lumin',
  authors: [{ name: 'Zistica Inc.' }],
  keywords: [
    'ai',
    'agent',
    'observability',
    'tracing',
    'langchain',
    'crewai',
    'local-first',
    'open source',
  ],
};

const VERSION = '0.1.0';

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        {/* Inline theme bootstrap — must run before <body> renders to
            avoid a dark→light flash on first paint. dangerouslySetInnerHTML
            is the documented Next.js pattern for this; the script string
            is a constant, no user input goes through it. */}
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      <body>
        <div className="min-h-screen flex flex-col">
          <PanicBanner />
          <header className="app-header px-6 py-3 flex items-center justify-between gap-4">
            <Link
              href="/agents"
              className="flex items-center gap-2.5 hover:opacity-90 transition-opacity"
            >
              <Logo className="h-6 w-6" />
              <span className="wordmark font-semibold tracking-tight text-base">
                Lumin
              </span>
              <span className="text-[var(--muted)] text-xs hidden sm:inline">
                local-first agent observability
              </span>
            </Link>

            <nav className="flex items-center gap-1 text-xs">
              <NavLink href="/agents">Agents</NavLink>
              <NavLink href="/traces">Traces</NavLink>
              <NavLink href="/sessions">Sessions</NavLink>
              <NavLink href="/policies">Policies</NavLink>
              <NavLink href="/decisions">Decisions</NavLink>
              <ViolationsNavLink />
              <span className="opacity-30 mx-1">·</span>
              <a
                href="/api/docs"
                target="_blank"
                rel="noreferrer"
                className="px-2 py-1 rounded text-[var(--muted)] hover:text-[var(--foreground)] hover:bg-[var(--background-hover)] transition-colors"
              >
                API
              </a>
              <a
                href="https://github.com/amitbidlan/zistica-lumin"
                target="_blank"
                rel="noreferrer"
                className="px-2 py-1 rounded text-[var(--muted)] hover:text-[var(--foreground)] hover:bg-[var(--background-hover)] transition-colors"
              >
                GitHub
              </a>
              <ThemeToggle />
              <span
                className="ml-1 font-mono text-[10px] uppercase tracking-wider px-1.5 py-0.5 border border-[var(--border)] rounded text-[var(--muted)]"
                title={`Lumin ${VERSION}`}
              >
                v{VERSION}
              </span>
            </nav>
          </header>

          <main className="px-6 py-8 flex-1">{children}</main>

          <footer className="border-t border-[var(--border)] px-6 py-3 text-[11px] text-[var(--muted)] flex items-center justify-between gap-4 flex-wrap">
            <span className="flex items-center gap-2">
              <span
                className="inline-block h-1.5 w-1.5 rounded-full bg-emerald-500"
                aria-hidden
              />
              Lumin runs locally · no telemetry, no cloud sync.
            </span>
            <span className="font-mono">
              Apache 2.0 · Zistica Inc. · 2026
            </span>
          </footer>
        </div>
      </body>
    </html>
  );
}
