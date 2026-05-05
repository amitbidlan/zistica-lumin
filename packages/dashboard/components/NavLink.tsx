'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';

/**
 * Top-nav link with an active-state highlight. The header is sticky so
 * it's always visible — knowing where you are matters.
 */
export default function NavLink({
  href,
  children,
}: {
  href: string;
  children: React.ReactNode;
}) {
  const pathname = usePathname();
  const isActive = pathname === href || pathname.startsWith(`${href}/`);
  return (
    <Link
      href={href}
      className={
        'px-2 py-1 rounded transition-colors ' +
        (isActive
          ? 'text-[var(--foreground)] bg-[var(--background-hover)]'
          : 'text-[var(--muted)] hover:text-[var(--foreground)] hover:bg-[var(--background-hover)]')
      }
    >
      {children}
    </Link>
  );
}
