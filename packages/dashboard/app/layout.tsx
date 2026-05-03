import './globals.css';
import type { Metadata } from 'next';
import Link from 'next/link';
import Logo from '@/components/Logo';

export const metadata: Metadata = {
  title: {
    default: 'Lumin — local-first AI agent observability',
    template: '%s · Lumin',
  },
  description:
    'Add 2 lines of code, see every step your agent takes. Runs entirely on your laptop. No account, no credit card, no data leaves your machine.',
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
    <html lang="en">
      <body>
        <div className="min-h-screen flex flex-col">
          <header className="border-b border-[var(--border)] px-6 py-3 flex items-center justify-between gap-4">
            <Link
              href="/traces"
              className="flex items-center gap-2.5 hover:opacity-90 transition-opacity"
            >
              <Logo className="h-6 w-6" />
              <span className="font-semibold tracking-tight text-base">
                Lumin
              </span>
              <span className="text-[var(--muted)] text-xs hidden sm:inline">
                local-first agent observability
              </span>
            </Link>

            <nav className="flex items-center gap-5 text-xs">
              <Link
                href="/traces"
                className="text-[var(--muted)] hover:text-[var(--foreground)] transition-colors"
              >
                Traces
              </Link>
              <Link
                href="/sessions"
                className="text-[var(--muted)] hover:text-[var(--foreground)] transition-colors"
              >
                Sessions
              </Link>
              <a
                href="/api/docs"
                target="_blank"
                rel="noreferrer"
                className="text-[var(--muted)] hover:text-[var(--foreground)] transition-colors"
              >
                API
              </a>
              <a
                href="https://github.com/zistica/lumin"
                target="_blank"
                rel="noreferrer"
                className="text-[var(--muted)] hover:text-[var(--foreground)] transition-colors"
              >
                GitHub
              </a>
              <span
                className="font-mono text-[10px] uppercase tracking-wider px-1.5 py-0.5 border border-[var(--border)] rounded text-[var(--muted)]"
                title={`Lumin ${VERSION}`}
              >
                v{VERSION}
              </span>
            </nav>
          </header>

          <main className="px-6 py-6 flex-1">{children}</main>

          <footer className="border-t border-[var(--border)] px-6 py-3 text-[11px] text-[var(--muted)] flex items-center justify-between gap-4 flex-wrap">
            <span className="flex items-center gap-2">
              <span
                className="inline-block h-1.5 w-1.5 rounded-full bg-emerald-500"
                aria-hidden
              />
              Your data never leaves this machine.
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
